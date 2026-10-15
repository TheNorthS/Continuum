import json  # 将事实检查反馈编码为文本，供内容审查读取。
from typing import TypedDict  # 声明图状态字段；运行时状态仍是普通字典。

from langgraph.graph import END, START, StateGraph  # 图的终点、起点和状态图构建器。

from app.common.context import knowledge_scope  # 请求级知识库范围，避免并发请求互相影响。
from app.memory.context import ACTIVE_CONTEXT
from app.memory.retrieval import render as render_memory


class RAGState(TypedDict, total=False):  # total=False：节点可只返回自己更新的字段。
    use_knowledge: bool  # 是否允许检索知识库；关闭后仍可使用所选附件。
    attachment_sources: list[dict]  # ChatService 准备好的附件文本和视觉识别证据。
    original_question: str  # 用户原始问题，始终作为生成及有效性判断的目标。
    query: str  # 用于检索的独立问题，允许指代消解和有限次改写。
    history: list[dict]  # 已按权限、范围边界及长度裁剪的历史消息。
    mode: str  # adaptive 使用普通路由，agentic 使用模型工具调用路由。
    route: str  # 当前选中的下一个节点名。
    sources: list[dict]  # 本次候选或已筛选的证据，顺序决定引用序号。
    answer: str  # 最新候选答案；通过最终门控前不会直接发送给用户。
    rewrites: int  # 本轮已经执行的问题改写次数。
    generations: int  # 本轮已经生成候选答案的次数，不是历史对话轮数。
    web_used: bool  # 本轮是否进入过网络搜索，防止重复联网。
    supported: bool  # Grounding 判断答案中的事实与引用是否有资料依据。
    grounding_feedback: list[dict]  # 上一版答案缺少依据的陈述和原因，用于修正。
    useful: bool  # 答案在有依据的前提下，是否解决用户原始问题。
    status: str  # 最终结果状态，例如 answered、insufficient、general。


# 集中维护工作流节点名称，为工作流事件提供可读的中文说明。
LABELS = {
    "general": "不使用知识库作答",
    "route": "理解问题并选择数据源",
    "retrieve": "混合检索知识库",
    "grade_documents": "评估资料相关性",
    "rewrite": "改写检索问题",
    "web": "查询网络资料",
    "generate": "根据资料生成回答",
    "grade_answer": "检查事实依据和回答有效性",
    "abstain": "资料不足，停止生成",
    "direct": "回复日常对话",
}


class Orchestrator:
    """Two modes share bounded correction nodes and injected, testable service adapters."""

    def __init__(self, settings, llm, knowledge, web, tracing=None, moderation=None):
        # 保存注入的配置与服务；编排层不自行创建数据库、模型客户端或检索引擎。
        self.settings, self.llm, self.knowledge, self.web_search = settings, llm, knowledge, web
        # 内容审查可在独立单元测试中省略，正式 Container 会注入实际服务。
        self.moderation = moderation
        # 创建执行图构建器
        workflow = StateGraph(RAGState)
        # 批量注册节点
        for name in LABELS:
            # 节点名称与同名实例方法对应，例如 generate → self.generate。
            action = getattr(self, name)
            # 有追踪服务时，在每个节点执行前后记录 span。
            if tracing:
                # 用默认参数绑定本轮 action/name，避免闭包在循环结束后都指向最后一个节点。
                async def instrumented(state, action=action, name=name):
                    # span 记录节点耗时、父子关系和成功/异常状态。
                    async with tracing.span("workflow." + name):
                        # 执行实际节点，返回要合并进图状态的字段。
                        return await action(state)

                # 把带追踪的包装函数注册为节点。
                workflow.add_node(name, instrumented)
            else:
                # 未配置追踪时，直接注册原节点方法。
                workflow.add_node(name, action)
        # 所有问答首先进入 route，决定普通对话、检索或联网。
        workflow.add_edge(START, "route")
        # 根据 route 节点写入的 state["route"] 选择分支，映射中的键和值均为节点名。
        workflow.add_conditional_edges(
            "route", lambda state: state["route"], {n: n for n in ("retrieve", "web", "direct", "general")}
        )
        workflow.add_edge("retrieve", "grade_documents")  # 获取证据后先做相关性筛选。
        # 筛选后可能生成答案、改写查询、联网补充，或因资料不足停止。
        workflow.add_conditional_edges(
            "grade_documents", self.after_documents, {n: n for n in ("generate", "rewrite", "web", "abstain")}
        )
        workflow.add_edge("rewrite", "retrieve")  # 问题改写后重新检索，而非直接生成。
        # 网络资料存在且生成额度未耗尽时才生成，否则进入拒答节点。
        workflow.add_conditional_edges("web", self.after_web, {"generate": "generate", "abstain": "abstain"})
        workflow.add_edge("generate", "grade_answer")  # 每一版候选答案都必须重新做质量检查。
        # 答案检查决定结束、带反馈修正、改写检索、联网或拒答。
        workflow.add_conditional_edges(
            "grade_answer", self.after_answer, {n: n for n in ("generate", "rewrite", "web", "abstain", END)}
        )
        workflow.add_edge("direct", END)  # 打招呼等直接回复完成后结束图执行。
        workflow.add_edge("general", END)  # 普通对话不走资料 Grounding，但仍接受内容审查。
        workflow.add_edge("abstain", END)  # 资料不足的固定回复结束本轮尝试。
        # 编译工作流，得到可以执行的图对象
        self.graph = workflow.compile()

    async def route(self, state):
        # 每轮从原始问题开始，不用旧的改写文本替代用户意图。
        query = state["original_question"]
        # 知识库关闭且未提供附件时，直接进入普通对话，不执行任何检索。
        if not state.get("use_knowledge", True) and not state.get("attachment_sources"):
            return {"query": query, "route": "general"}
        # 指代消解
        if state["history"]:
            # 例如结合历史把“它支持什么索引”改成“Milvus 支持什么索引”。
            query = await self.llm.call("contextualize", {"question": query, "history": state["history"]})
        # 是否设置了知识库作用域
        if knowledge_scope.get() is not None or state.get("attachment_sources"):
            # 显式范围或附件问答固定进入 retrieve；该节点内部再判断是否真的搜索知识库。
            # 将查询限制为 4000 字符，和入口问题及下游检索的长度边界保持一致。
            return {"query": query[:4000], "route": "retrieve"}
        # 只有未限定范围、没有附件的兼容路径才允许模型自由选择数据源。
        value = await self.llm.call(
            "agent" if state["mode"] == "agentic" else "route",  # 选择工具调用或 JSON 路由任务。
            {"question": query, "knowledge_domain": self.settings.knowledge_domain},  # 给出问题和库领域描述。
        )
        # Agent 可以给出工具查询；没有新查询时沿用当前 query。
        return {"query": value.get("query", query)[:4000], "route": value["route"]}

    async def retrieve(self, state):
        # 复制附件列表，避免追加知识库结果时修改原 attachment_sources。
        sources = list(state.get("attachment_sources", []))
        # 记忆来源先经权限过滤与 Token 预算选择，保留独立标识，不能伪装为知识库文档。
        memory_context = ACTIVE_CONTEXT.get()
        if memory_context:
            sources += [
                {
                    "id": "memory-" + m["id"],
                    "text": render_memory(m),
                    "score": m.get("score", 1.0),
                    "metadata": {
                        "memory_id": m["id"],
                        "title": "个人记忆：" + m["payload"]["subject"],
                        "category": "memory",
                    },
                }
                for m in memory_context["memories"]
            ]
        # 关闭知识库时完全跳过此调用，因此不会为问题生成检索向量。
        if state.get("use_knowledge", True):
            # KnowledgeService 完成授权范围内的检索与精排，返回 Top-K 片段。
            sources += await self.knowledge.search(state["query"], self.settings.retrieval_top_k)
        if self.moderation:
            # 统一审查附件和知识库来源，移除不安全片段。
            sources = await self.moderation.filter_sources(sources)
        # 新一批资料使旧 Grounding 反馈失效，必须清空。
        return {"sources": sources, "grounding_feedback": []}

    async def grade_documents(self, state):
        sources = []  # 收集最终保留的相关片段。
        grades = []  # 按输入顺序保存模型的相关性判断。
        # 分批调用，避免一次提交的任务数量超过 LLMService.batch 的上限。
        for start in range(0, len(state["sources"]), self.settings.llm_batch_size):
            # 取当前批次，最后一批可以不足 batch_size。
            batch = state["sources"][start : start + self.settings.llm_batch_size]
            # batch 内可并发执行，返回结果仍按输入顺序排列。
            grades.extend(
                await self.llm.batch(
                    "relevance",  # 只判断片段是否有助于回答问题，不在这里生成答案。
                    [{"question": state["query"], "document": source["text"]} for source in batch],
                )
            )
        for source, grade in zip(state["sources"], grades, strict=True):  # 数量不一致时立即报错。
            if grade["relevant"]:
                # 保持原相对顺序，后续引用序号与生成时看到的顺序一致。
                sources.append(source)
        # 只覆盖 sources，不改变本轮生成或改写计数。
        return {"sources": sources}

    def correction(self, state):
        # 附件问答及关闭知识库的问答不会自动改写检索或转向联网扩大范围。
        if not state.get("use_knowledge", True) or state.get("attachment_sources"):
            return "abstain"
        # 尚未联网且还有改写额度时，优先尝试更合适的知识库查询。
        if not state["web_used"] and state["rewrites"] < self.settings.max_rewrites:
            return "rewrite"
        # 改写用尽后，仅未限定知识库且显式启用联网的路径允许一次网络补充。
        if not state["web_used"] and self.settings.web_search_enabled and knowledge_scope.get() is None:
            return "web"
        # 没有合法补救路径时停止，不制造没有依据的回答。
        return "abstain"

    def after_documents(self, state):
        # 答案生成次数是否达到上限
        if state["generations"] >= self.settings.max_generations:
            return "abstain"
        # 有资料才生成；没有资料则交由 correction 选择补救路径。
        return "generate" if state["sources"] else self.correction(state)

    async def rewrite(self, state):
        # 同时提供原始意图和当前查询，避免连续改写偏离用户目标。
        query = await self.llm.call(
            "rewrite", {"original_question": state["original_question"], "query": state["query"]}
        )
        return {
            "query": query[:4000],  # 限制改写输出长度。
            "rewrites": state["rewrites"] + 1,  # 记录实际执行过的一次改写。
            "sources": [],  # 丢弃旧候选，下一步重新检索。
            "grounding_feedback": [],  # 旧答案的反馈不能套用在新资料上。
        }

    async def web(self, state):
        # 再次校验范围，防止调用者直接调用此节点绕过路由限制。
        if knowledge_scope.get() is not None:
            return {"sources": [], "web_used": False, "grounding_feedback": []}
        # 调用配置的 Tavily、Exa 或双路融合服务。
        sources = await self.web_search.search(state["query"])
        if self.moderation:
            # 网络来源与知识库来源使用相同的内容安全门控。
            sources = await self.moderation.filter_sources(sources)
        # 即使零命中也记录已经联网，避免下一次纠正又重复搜索。
        return {"sources": sources, "web_used": True, "grounding_feedback": []}

    def after_web(self, state):
        # 网络结果非空且仍有生成额度才进入 generate，否则返回资料不足。
        return (
            "generate"
            if state["sources"] and state["generations"] < self.settings.max_generations
            else "abstain"
        )

    async def generate(self, state):
        # 首次生成只使用原始问题、有效历史和本轮证据。
        payload = {
            "original_question": state["original_question"],  # 回答用户问题，而不是只回答改写后的检索词。
            "history": state["history"],  # 用于理解对话，不替代来源证据。
            "sources": state["sources"],  # 模型提示词要求按此列表的序号给出引用。
        }
        # 第一次生成没有反馈，Grounding 不通过后才会出现。
        feedback = state.get("grounding_feedback", [])
        if feedback:
            # 评分器输出仍是不可信的模型文本，传入下一个模型前必须审查。
            if self.moderation:
                await self.moderation.guard(json.dumps(feedback, ensure_ascii=False), "output")
            # 提供上一版答案及具体依据缺口，使这次重试成为有反馈的修正。
            payload.update(previous_answer=state["answer"], grounding_feedback=feedback)
        # 获取完整候选答案，暂不向用户推送正文。
        answer = await self.llm.call("generate", payload)
        if self.moderation:
            # 候选答案先过安全审查，再送给 Grounding 与有效性评分器。
            await self.moderation.guard(answer, "output")
        # 计入一次生成，并清空已消费的反馈；下一节点会给出针对新版答案的新反馈。
        return {"answer": answer, "generations": state["generations"] + 1, "grounding_feedback": []}

    async def grade_answer(self, state):
        # 答案是否有资料依据
        grounding = await self.llm.call("grounding", {"sources": state["sources"], "answer": state["answer"]})
        supported = grounding["supported"]  # LLMService 已校验布尔值及 issues 的结构一致性。
        # 使用短路逻辑：事实依据不通过时，不再浪费一次有效性判断调用。
        useful = (
            supported
            and (
                # 答案是否真正回答问题
                await self.llm.call(
                    "usefulness", {"original_question": state["original_question"], "answer": state["answer"]}
                )
            )["useful"]
        )
        return {
            "supported": supported,  # 供 after_answer 决定是否修正。
            "grounding_feedback": grounding["issues"] if not supported else [],  # 仅保存失败反馈。
            "useful": useful,  # 有依据但未解决问题时，走检索纠正而非同资料重写。
            "status": "demo" if self.settings.mode == "demo" else "answered",  # 若最终拒答，后续节点会覆盖。
        }

    def after_answer(self, state):
        # 两项均通过即可结束；最后一次允许的生成也有机会正常交付。
        if state["supported"] and state["useful"]:
            return END
        # 质量不通过且已达到上限时直接拒答，不能继续绕图重试。
        if state["generations"] >= self.settings.max_generations:
            return "abstain"
        # 事实依据失败时，携带反馈并使用当前资料修正答案。
        if not state["supported"]:
            return "generate"
        # 事实有依据但不解决问题，尝试改写/联网；范围不允许时拒答。
        return self.correction(state)

    async def abstain(self, state):
        # 覆盖被否决的候选答案，清除其来源和反馈，避免它们被最终结果导出。
        return {
            "answer": "现有资料不足以可靠回答这个问题。请补充相关文档或缩小问题范围。",
            "status": "insufficient",
            "sources": [],
            "grounding_feedback": [],
        }

    async def direct(self, state):
        # Agent 路由的简短问候/能力介绍，不是普通知识问题的兜底回答。
        answer = await self.llm.call("direct", {"question": state["original_question"]})
        if self.moderation:
            # 无检索来源也不能跳过输出安全检查。
            await self.moderation.guard(answer, "output")
        # 此分支没有检索证据，因此不返回虚构来源。
        return {
            "answer": answer,
            "status": "direct",
            "sources": [],
        }

    async def general(self, state):
        # 用户明确关闭知识库且没有附件时，使用主 LLM 进行普通多轮对话。
        answer = await self.llm.call(
            "general", {"question": state["original_question"], "history": state["history"]}
        )
        if self.moderation:
            # 普通对话仍执行同样的输出内容审查。
            await self.moderation.guard(answer, "output")
        # 用独立 general 状态区分基于资料的回答，并明确来源为空。
        return {"answer": answer, "status": "general", "sources": [], "generations": 1}

    async def run(self, question, history, mode, emit=None, *, attachment_sources=None, use_knowledge=True):
        # 每次调用创建新的状态，历史轮次的重试计数和失败反馈不会延续。
        state = RAGState(
            attachment_sources=attachment_sources or [],  # 固定本轮选定的附件证据。
            use_knowledge=use_knowledge,  # 保留用户的知识库开关。
            original_question=question,  # 生成目标始终不变。
            query=question,  # 尚未进行指代消解或检索改写。
            history=history,  # ChatService 已准备好安全上下文。
            mode=mode,  # 记录 adaptive / agentic 模式。
            rewrites=0,  # 改写次数从零开始。
            generations=0,  # 候选生成次数从零开始。
            web_used=False,  # 本轮尚未联网。
            grounding_feedback=[],  # 本轮尚无失败反馈。
            sources=[],  # 来源由 retrieve 或 web 节点填充。
        )
        trace = []  # 保存面向前端的简短步骤，不保存中间答案正文。
        # updates 模式逐节点返回状态增量；48 是图执行步数的额外保护上限。
        async for updates in self.graph.astream(state, stream_mode="updates", config={"recursion_limit": 48}):
            # 一次更新可能包含多个节点，逐个合并并发出步骤事件。
            for node, changes in updates.items():
                state.update(changes)  # 本地快照用于生成事件和最终返回值。
                event = {
                    "node": node,  # 程序可识别的节点名。
                    "label": LABELS[node],  # 用户可读的中文步骤描述。
                    "sources": len(state["sources"]),  # 仅公开来源数量，不提前发送来源正文。
                    "rewrites": state["rewrites"],  # 当前已完成的改写次数。
                    "generations": state["generations"],  # 当前已完成的生成次数。
                }
                trace.append(event)  # 普通 JSON 接口也能查看执行过程。
                if emit:
                    await emit(event)  # SSE 回调可能等待队列空间，保留异步背压。
        # 只导出允许交付的字段；内部 query、history 和 grounding_feedback 不在结果中。
        return {
            "answer": state["answer"],
            "sources": state["sources"],
            "trace": trace,
            "status": state["status"],
            "mode": mode,
            "runtime": self.settings.mode,
            "rewrites": state["rewrites"],
            "generations": state["generations"],
        }
