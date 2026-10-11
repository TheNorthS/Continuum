import asyncio
import json
from dataclasses import dataclass

import httpx
from prometheus_client import Counter

from app.common.errors import AppError
from app.safety.local_rules import classify
from app.safety.policy import CATEGORIES, POLICY_VERSION, SYSTEM_PROMPT, Scores


@dataclass(frozen=True)
class Verdict:
    scores: dict[str, float]
    categories: tuple[str, ...]
    backend: str

    @property
    def blocked(self):
        return bool(self.categories)


class ModerationBlocked(Exception):
    def __init__(self, stage, verdict):
        super().__init__("Content blocked")
        self.stage, self.verdict = stage, verdict


class ModerationService:
    def __init__(self, settings, security, monitoring, tracing):
        self.settings, self.security, self.monitoring, self.tracing = settings, security, monitoring, tracing
        self.gate = asyncio.Semaphore(settings.moderation_max_concurrency)
        self.http = httpx.AsyncClient(
            timeout=settings.moderation_timeout,
            limits=httpx.Limits(
                max_connections=settings.moderation_max_concurrency,
                max_keepalive_connections=settings.moderation_max_concurrency,
            ),
        )
        self.decisions = Counter(
            "rag_e_moderation_decisions_total",
            "Content moderation decisions",
            ["stage", "backend", "decision"],
            registry=monitoring.registry,
        )
        self.category_blocks = Counter(
            "rag_e_moderation_blocks_total",
            "Blocked categories",
            ["stage", "category"],
            registry=monitoring.registry,
        )

    def status(self):
        return {
            "enabled": True,
            "backend": self.settings.effective_moderation_backend,
            "policy_version": POLICY_VERSION,
            "thresholds": self.settings.moderation_thresholds,
            "failure_mode": "closed",
        }

    async def _remote(self, text, stage):
        async with self.gate:
            base = self.settings.moderation_base_url or self.settings.llm_base_url
            key = (
                self.settings.moderation_api_key.get_secret_value()
                or self.settings.llm_api_key.get_secret_value()
            )
            response = await self.http.post(
                base.rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json={
                    **(
                        {"thinking": {"type": self.settings.moderation_thinking}}
                        if self.settings.moderation_thinking != "default"
                        else {}
                    ),
                    "model": self.settings.moderation_model or self.settings.llm_model,
                    "temperature": 0,
                    "max_tokens": 400,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": json.dumps({"stage": stage, "text": text}, ensure_ascii=False),
                        },
                    ],
                },
            )
            response.raise_for_status()
            payload = response.json()
            choice = payload["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError("incomplete moderation")
            return Scores.model_validate_json(choice["message"]["content"]).model_dump()

    async def _classify(self, text, stage):
        scores = await asyncio.to_thread(classify, text)
        if any(scores[c] >= self.settings.moderation_thresholds[c] for c in CATEGORIES):
            return Verdict(
                scores,
                tuple(c for c in CATEGORIES if scores[c] >= self.settings.moderation_thresholds[c]),
                "local",
            )
        if self.settings.effective_moderation_backend == "llm":
            size = self.settings.moderation_chunk_chars
            # Every character is reviewed; overlap limits boundary splitting. No silent truncation.
            for start in range(0, len(text), size - 300):
                remote = await self._remote(text[start : start + size], stage)
                scores = {category: max(scores[category], remote[category]) for category in CATEGORIES}
        return Verdict(
            scores,
            tuple(c for c in CATEGORIES if scores[c] >= self.settings.moderation_thresholds[c]),
            self.settings.effective_moderation_backend,
        )

    async def check(self, text, stage):
        if stage not in {"input", "source", "output"}:
            raise ValueError("Unknown moderation stage")
        if not isinstance(text, str) or len(text) > self.settings.moderation_max_chars:
            self.decisions.labels(stage, self.settings.effective_moderation_backend, "error").inc()
            await asyncio.to_thread(
                self.security.audit,
                "moderation.limit",
                outcome="denied",
                details={"stage": stage, "policy": POLICY_VERSION},
            )
            raise AppError("moderation_limit", "待审内容超过安全审查范围，请缩短输入或资料", 413)
        try:
            async with self.tracing.span(
                "moderation", kind="guardrail", metadata={"stage": stage, "policy": POLICY_VERSION}
            ):
                async with asyncio.timeout(self.settings.moderation_check_timeout):
                    verdict = await self._classify(text, stage)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.decisions.labels(stage, self.settings.effective_moderation_backend, "error").inc()
            await asyncio.to_thread(
                self.security.audit,
                "moderation.error",
                outcome="denied",
                details={"stage": stage, "policy": POLICY_VERSION, "error_type": type(exc).__name__},
            )
            raise AppError(
                "moderation_unavailable", "内容安全审查暂不可用，本次问答已停止，请稍后重试", 503
            ) from exc
        outcome = "blocked" if verdict.blocked else "allowed"
        self.decisions.labels(stage, verdict.backend, outcome).inc()
        for category in verdict.categories:
            self.category_blocks.labels(stage, category).inc()
        # Do not log text, excerpts, provider response bodies, or model-supplied reasons.
        await asyncio.to_thread(
            self.security.audit,
            "moderation." + outcome,
            outcome="denied" if verdict.blocked else "ok",
            details={
                "stage": stage,
                "backend": verdict.backend,
                "policy": POLICY_VERSION,
                "categories": list(verdict.categories),
                "scores": verdict.scores,
                "thresholds": self.settings.moderation_thresholds,
            },
        )
        return verdict

    async def guard(self, text, stage):
        verdict = await self.check(text, stage)
        if verdict.blocked:
            raise ModerationBlocked(stage, verdict)
        return verdict

    async def filter_sources(self, sources):
        retained = []
        for source in sources:
            content = source["text"] + "\n" + "\n".join(str(v) for v in source.get("metadata", {}).values())
            if not (await self.check(content, "source")).blocked:
                retained.append(source)
        # Catch risks which only arise by joining multiple individually benign excerpts.
        if retained:
            await self.guard("\n\n".join(s["text"] for s in retained), "source")
        return retained

    async def guard_sources(self, sources):
        await self.guard(
            "\n\n".join(
                source["text"] + "\n" + "\n".join(str(v) for v in source.get("metadata", {}).values())
                for source in sources
            ),
            "source",
        )

    @staticmethod
    def blocked_result(blocked, mode, runtime, conversation_id):
        if "self_harm" in blocked.verdict.categories and blocked.stage == "input":
            answer = "听起来你可能正在经历很难熬的时刻。我不能提供伤害自己的方法。如果你此刻可能伤害自己，请先远离危险物品，联系身边可信任的人陪伴你，并尽快联系当地急救或危机援助服务。也可以告诉我，你现在是否安全。"
        else:
            answer = (
                "本次内容未通过安全审查，我无法提供这类内容。你可以改为询问相关的预防、防护或合法合规知识。"
            )
        return {
            "answer": answer,
            "sources": [],
            "trace": [],
            "status": "blocked",
            "mode": mode,
            "runtime": runtime,
            "conversation_id": conversation_id,
            "rewrites": 0,
            "generations": 0,
            "safety": {
                "decision": "blocked",
                "stage": blocked.stage,
                "policy_version": POLICY_VERSION,
                "backend": blocked.verdict.backend,
            },
        }

    async def close(self):
        await self.http.aclose()
