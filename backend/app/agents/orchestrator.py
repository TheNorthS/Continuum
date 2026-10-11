from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.common.context import knowledge_scope


class RAGState(TypedDict, total=False):
    original_question: str
    query: str
    history: list[dict]
    mode: str
    route: str
    sources: list[dict]
    answer: str
    rewrites: int
    generations: int
    web_used: bool
    supported: bool
    useful: bool
    status: str


LABELS = {
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
        self.settings, self.llm, self.knowledge, self.web_search = settings, llm, knowledge, web
        self.moderation = moderation
        workflow = StateGraph(RAGState)
        for name in LABELS:
            action = getattr(self, name)
            if tracing:

                async def instrumented(state, action=action, name=name):
                    async with tracing.span("workflow." + name):
                        return await action(state)

                workflow.add_node(name, instrumented)
            else:
                workflow.add_node(name, action)
        workflow.add_edge(START, "route")
        workflow.add_conditional_edges(
            "route", lambda state: state["route"], {n: n for n in ("retrieve", "web", "direct")}
        )
        workflow.add_edge("retrieve", "grade_documents")
        workflow.add_conditional_edges(
            "grade_documents", self.after_documents, {n: n for n in ("generate", "rewrite", "web", "abstain")}
        )
        workflow.add_edge("rewrite", "retrieve")
        workflow.add_conditional_edges("web", self.after_web, {"generate": "generate", "abstain": "abstain"})
        workflow.add_edge("generate", "grade_answer")
        workflow.add_conditional_edges(
            "grade_answer", self.after_answer, {n: n for n in ("generate", "rewrite", "web", "abstain", END)}
        )
        workflow.add_edge("direct", END)
        workflow.add_edge("abstain", END)
        self.graph = workflow.compile()

    async def route(self, state):
        query = state["original_question"]
        if state["history"]:
            query = await self.llm.call("contextualize", {"question": query, "history": state["history"]})
        if knowledge_scope.get() is not None:
            return {"query": query[:4000], "route": "retrieve"}
        value = await self.llm.call(
            "agent" if state["mode"] == "agentic" else "route",
            {"question": query, "knowledge_domain": self.settings.knowledge_domain},
        )
        return {"query": value.get("query", query)[:4000], "route": value["route"]}

    async def retrieve(self, state):
        sources = await self.knowledge.search(state["query"], self.settings.retrieval_top_k)
        if self.moderation:
            sources = await self.moderation.filter_sources(sources)
        return {"sources": sources}

    async def grade_documents(self, state):
        sources = []
        grades = []
        for start in range(0, len(state["sources"]), self.settings.llm_batch_size):
            batch = state["sources"][start : start + self.settings.llm_batch_size]
            grades.extend(
                await self.llm.batch(
                    "relevance",
                    [{"question": state["query"], "document": source["text"]} for source in batch],
                )
            )
        for source, grade in zip(state["sources"], grades, strict=True):
            if grade["relevant"]:
                sources.append(source)
        return {"sources": sources}

    def correction(self, state):
        if not state["web_used"] and state["rewrites"] < self.settings.max_rewrites:
            return "rewrite"
        if not state["web_used"] and self.settings.web_search_enabled and knowledge_scope.get() is None:
            return "web"
        return "abstain"

    def after_documents(self, state):
        if state["generations"] >= self.settings.max_generations:
            return "abstain"
        return "generate" if state["sources"] else self.correction(state)

    async def rewrite(self, state):
        query = await self.llm.call(
            "rewrite", {"original_question": state["original_question"], "query": state["query"]}
        )
        return {"query": query[:4000], "rewrites": state["rewrites"] + 1, "sources": []}

    async def web(self, state):
        if knowledge_scope.get() is not None:
            return {"sources": [], "web_used": False}
        sources = await self.web_search.search(state["query"])
        if self.moderation:
            sources = await self.moderation.filter_sources(sources)
        return {"sources": sources, "web_used": True}

    def after_web(self, state):
        return (
            "generate"
            if state["sources"] and state["generations"] < self.settings.max_generations
            else "abstain"
        )

    async def generate(self, state):
        answer = await self.llm.call(
            "generate",
            {
                "original_question": state["original_question"],
                "history": state["history"],
                "sources": state["sources"],
            },
        )
        if self.moderation:
            await self.moderation.guard(answer, "output")
        return {"answer": answer, "generations": state["generations"] + 1}

    async def grade_answer(self, state):
        supported = (
            await self.llm.call("grounding", {"sources": state["sources"], "answer": state["answer"]})
        )["supported"]
        useful = (
            supported
            and (
                await self.llm.call(
                    "usefulness", {"original_question": state["original_question"], "answer": state["answer"]}
                )
            )["useful"]
        )
        return {
            "supported": supported,
            "useful": useful,
            "status": "demo" if self.settings.mode == "demo" else "answered",
        }

    def after_answer(self, state):
        if state["supported"] and state["useful"]:
            return END
        if state["generations"] >= self.settings.max_generations:
            return "abstain"
        if not state["supported"]:
            return "generate"
        return self.correction(state)

    async def abstain(self, state):
        return {
            "answer": "现有资料不足以可靠回答这个问题。请补充相关文档或缩小问题范围。",
            "status": "insufficient",
            "sources": [],
        }

    async def direct(self, state):
        answer = await self.llm.call("direct", {"question": state["original_question"]})
        if self.moderation:
            await self.moderation.guard(answer, "output")
        return {
            "answer": answer,
            "status": "direct",
            "sources": [],
        }

    async def run(self, question, history, mode, emit=None):
        state = RAGState(
            original_question=question,
            query=question,
            history=history,
            mode=mode,
            rewrites=0,
            generations=0,
            web_used=False,
            sources=[],
        )
        trace = []
        async for updates in self.graph.astream(state, stream_mode="updates", config={"recursion_limit": 48}):
            for node, changes in updates.items():
                state.update(changes)
                event = {
                    "node": node,
                    "label": LABELS[node],
                    "sources": len(state["sources"]),
                    "rewrites": state["rewrites"],
                    "generations": state["generations"],
                }
                trace.append(event)
                if emit:
                    await emit(event)
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
