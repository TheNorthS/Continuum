import asyncio
import json
import re
from contextlib import nullcontext

from app.agents.grounding import GroundingVerdict
from app.common.errors import AppError
from app.infrastructure.http import post_json
from app.knowledge.rag.embedding import tokenize
from app.memory.schemas import Extraction as MemoryExtraction
from app.memory.schemas import Relation, Verification, WorkingSummary
from app.prompts.rag import BASE, PROMPTS

# Candidate answers and grader feedback remain private even with content tracing enabled.
PRIVATE_CONTENT_TASKS = frozenset(
    {
        "generate",
        "direct",
        "general",
        "grounding",
        "usefulness",
        "contextualize",
        "route",
        "agent",
        "rewrite",
        "memory_compact",
        "memory_extract",
        "memory_relation",
        "memory_verify",
    }
)


class LLMService:
    def __init__(self, settings, client, tracing=None):
        self.settings, self.client = settings, client
        self.tracing = tracing
        self.gate = asyncio.Semaphore(settings.llm_max_concurrency)
        self.context = None
        self.token_meter = None

    async def call(self, task, payload):
        # Compaction may itself call the LLM, so it must happen before taking the model semaphore.
        original = payload
        if self.context:
            payload = await self.context.prepare(task, payload, self.body)
        try:
            return await self._dispatch(task, payload)
        except AppError as exc:
            if (
                self.context
                and context_overflow(exc)
                and await self.context.recover(task, original, self.body)
            ):
                payload = await self.context.prepare(task, original, self.body)
                return await self._dispatch(task, payload)  # Exactly one confirmed-overflow retry.
            raise

    async def _dispatch(self, task, payload):
        async with self.gate:
            context = (
                self.tracing.span(
                    "llm",
                    kind="generation",
                    model=self.settings.llm_model or "demo",
                    metadata={"task": task, "mode": self.settings.mode},
                    # Intermediate answers/feedback must not escape before the final gates.
                    input=None if task in PRIVATE_CONTENT_TASKS else payload,
                )
                if self.tracing
                else nullcontext()
            )
            if self.tracing:
                self.tracing.monitoring.inflight.inc()
            try:
                async with context as observation:
                    result = await self._call(task, payload, observation)
                    if observation and task not in PRIVATE_CONTENT_TASKS:
                        observation.update(output=result)
                    return result
            finally:
                if self.tracing:
                    self.tracing.monitoring.inflight.dec()

    async def batch(self, task, payloads):
        if len(payloads) > self.settings.llm_batch_size:
            raise AppError("batch_limit", "批量请求超过配置上限", 413)
        tasks = [asyncio.create_task(self.call(task, payload)) for payload in payloads]
        try:
            return await asyncio.gather(*tasks)
        finally:
            for pending in tasks:
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def body(self, task, payload):
        structured = task in {
            "route",
            "agent",
            "relevance",
            "grounding",
            "usefulness",
            "extract_graph",
            "memory_compact",
            "memory_extract",
            "memory_relation",
            "memory_verify",
        }
        body = {
            "model": self.settings.llm_model,
            "temperature": 0,
            "max_tokens": self.settings.context_summary_tokens
            if task == "memory_compact"
            else 4000
            if task in {"extract_graph", "memory_extract"}
            else 1600,
            "messages": [
                {"role": "system", "content": BASE + "\n" + PROMPTS[task]},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        }
        if self.settings.llm_thinking != "default":
            body["thinking"] = {"type": self.settings.llm_thinking}
        if task == "agent":
            body["messages"][0]["content"] = (
                BASE + "选择工具回答知识问题。只有打招呼或介绍自身能力时可以直接回答。"
            )
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    },
                }
                for name, description in [
                    ("rag_retriever", "搜索本地知识库，包含：" + self.settings.knowledge_domain),
                    ("web_search", "查询领域外或实时网络信息；未配置网络时将提示资料不足"),
                ]
            ]
            body["tool_choice"] = "auto"
            body["parallel_tool_calls"] = False
            structured = False
        if structured:
            body["response_format"] = {"type": "json_object"}
        return body

    async def _call(self, task, payload, observation=None):
        body = self.body(task, payload)
        if self.token_meter:
            size = await self.token_meter.count(body)
            if size > self.token_meter.limits(body["model"], body["max_tokens"])["hard"]:
                raise AppError(
                    "context_budget_exceeded", "模型输入超出安全预算，请缩小资料范围或压缩上下文", 413
                )
        if self.settings.mode == "demo":
            return await asyncio.to_thread(self._demo, task, payload)
        structured = "response_format" in body
        result = await post_json(
            self.client,
            self.settings.llm_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + self.settings.llm_api_key.get_secret_value()},
            json=body,
        )
        try:
            usage = result.get("usage") or {}
            counts = {"input": usage.get("prompt_tokens", 0), "output": usage.get("completion_tokens", 0)}
            if observation:
                observation.update(usage_details=counts)
                for name, count in counts.items():
                    if isinstance(count, int) and count >= 0:
                        self.tracing.monitoring.tokens.labels(name).inc(count)
            message = result["choices"][0]["message"]
            if task == "agent":
                calls = message.get("tool_calls") or []
                if not calls:
                    if not isinstance(message.get("content"), str) or not message["content"].strip():
                        raise ValueError("empty agent response")
                    return {"route": "direct"}
                name = calls[0]["function"]["name"]
                arguments = json.loads(calls[0]["function"]["arguments"])
                query = arguments["query"]
                if not isinstance(query, str) or not query.strip():
                    raise ValueError("invalid tool query")
                return {
                    "route": {"rag_retriever": "retrieve", "web_search": "web"}[name],
                    "query": query.strip()[:4000],
                }
            content = message["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty content")
            if not structured:
                return content.strip()
            value = json.loads(content)
            schemas = {
                "memory_compact": WorkingSummary,
                "memory_extract": MemoryExtraction,
                "memory_relation": Relation,
                "memory_verify": Verification,
            }
            if task in schemas:
                if result["choices"][0].get("finish_reason") != "stop":
                    raise ValueError("incomplete memory output")
                return schemas[task].model_validate(value).model_dump()
            if task == "grounding":
                return GroundingVerdict.model_validate(value).model_dump()
            if task == "extract_graph":
                from app.knowledge.graph.extraction import Extraction

                return Extraction.model_validate(value).model_dump()
            field = {
                "route": "route",
                "agent": "route",
                "relevance": "relevant",
                "usefulness": "useful",
            }[task]
            if task in {"route", "agent"}:
                allowed = {"retrieve", "web", "direct"} if task == "agent" else {"retrieve", "web"}
                if value[field] not in allowed:
                    raise ValueError("invalid route")
            elif type(value[field]) is not bool:
                raise ValueError("invalid grade")
            return value
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise AppError("llm_format", "模型输出格式不符合要求，请检查 JSON 模式支持", 502) from exc

    def _demo(self, task, data):
        if task == "memory_extract":
            return {"memories": []}  # Demo never pretends to infer stable facts semantically.
        if task == "memory_relation":
            return {"relation": "independent", "neighbor_id": None}
        if task == "memory_verify":
            return {"supported": True}
        if task == "memory_compact":
            previous = data.get("previous_summary")
            events = data["events"]
            last = events[-1]
            value = {"text": last["text"][:500], "evidence_ids": [last["id"]]}
            return {
                "original_goal": data["original_goal"],
                "current_status": value,
                "constraints": (previous or {}).get("constraints", []),
                "completed": [],
                "pending_tasks": [],
                "decisions": [],
                "errors": [],
                "next_action": {"text": "继续围绕原始目标回答后续问题", "evidence_ids": [last["id"]]},
            }
        if task == "general":
            return "【本地演示】本轮未使用知识库或网络。配置真实 LLM 后可进行普通对话，也可以添加附件围绕资料提问。"
        if task == "extract_graph":
            from app.knowledge.graph.extraction import demo_extract

            return demo_extract(data["text"])
        if task in {"route", "agent"}:
            question = data["question"].strip()
            if task == "agent" and question.lower() in {"你好", "hello", "hi"}:
                return {"route": "direct"}
            return {"route": "web" if re.search(r"今天.*天气|最新新闻", question) else "retrieve"}
        if task == "relevance":
            return {"relevant": bool(set(tokenize(data["question"])) & set(tokenize(data["document"])))}
        if task == "grounding":
            return {"supported": True, "issues": []}
        if task == "usefulness":
            return {"useful": True}
        if task == "contextualize":
            last = next((m["content"] for m in reversed(data["history"]) if m["role"] == "user"), "")
            return (
                last + " " + data["question"]
                if re.search(r"它|这个|上述|that|it\b", data["question"])
                else data["question"]
            )
        if task == "rewrite":
            return data["original_question"]
        if task == "direct":
            return "你好，我是知恒（Continuum）智能助手，可以结合知识库、附件与个人记忆回答问题，并延续多轮对话。当前为本地演示模式。"
        return "【本地演示：以下为资料摘录，未调用大模型或执行模型质量评分】\n\n" + "\n\n".join(
            f"{item['text'][:700]} [{i}]" for i, item in enumerate(data["sources"][:3], 1)
        )


def context_overflow(error):
    import httpx

    cause = error.__cause__
    if not isinstance(cause, httpx.HTTPStatusError) or cause.response.status_code not in {400, 413}:
        return False
    try:
        value = cause.response.json().get("error", {})
        return value.get("code") in {
            "context_length_exceeded",
            "context_window_exceeded",
            "max_context_length_exceeded",
        }
    except (ValueError, AttributeError, TypeError):
        return False
