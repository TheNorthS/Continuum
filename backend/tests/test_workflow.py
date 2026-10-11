from unittest.mock import AsyncMock

import pytest

from app.agents.orchestrator import Orchestrator

SOURCE = {"id": "one", "text": "EUV uses 13.5 nm", "score": 1, "metadata": {"source": "chip.md"}}


class ScriptedLLM:
    async def batch(self, task, payloads):
        return [await self.call(task, payload) for payload in payloads]

    def __init__(self, *, relevant=True, supported=True, useful=True, route="retrieve"):
        self.relevant, self.supported, self.useful, self.route = relevant, supported, useful, route
        self.calls = []

    async def call(self, task, payload):
        self.calls.append((task, payload))
        return {
            "route": {"route": self.route},
            "agent": {"route": self.route},
            "relevance": {"relevant": self.relevant},
            "grounding": {"supported": self.supported},
            "usefulness": {"useful": self.useful},
            "rewrite": "rewritten query",
            "contextualize": "standalone question",
            "generate": "13.5 nm [1]",
            "direct": "hello",
        }[task]


def build(settings, llm, sources=None):
    knowledge = AsyncMock()
    knowledge.search.return_value = sources if sources is not None else [SOURCE]
    web = AsyncMock()
    web.search.return_value = [SOURCE]
    return Orchestrator(settings, llm, knowledge, web), knowledge, web


async def test_irrelevant_docs_rewrite_then_web_once(settings):
    settings.web_search_enabled = True
    llm = ScriptedLLM(relevant=False)
    flow, knowledge, web = build(settings, llm)
    result = await flow.run("original intent", [], "adaptive")
    assert result["rewrites"] == 2
    assert knowledge.search.await_count == 3
    assert web.search.await_count == 1
    generation = next(payload for task, payload in llm.calls if task == "generate")
    assert generation["original_question"] == "original intent"
    assert result["sources"]


@pytest.mark.parametrize("supported,useful", [(False, True), (True, False)])
async def test_bad_answers_terminate_without_exposing_rejected_generation(settings, supported, useful):
    llm = ScriptedLLM(supported=supported, useful=useful)
    flow, _, _ = build(settings, llm)
    result = await flow.run("question", [], "adaptive")
    assert result["status"] == "insufficient"
    assert result["generations"] <= settings.max_generations
    assert result["rewrites"] <= settings.max_rewrites
    assert "13.5" not in result["answer"]
    assert result["sources"] == []
    assert len(result["trace"]) < 30


async def test_no_sources_no_web_does_not_invent_answer(settings):
    flow, _, web = build(settings, ScriptedLLM(), sources=[])
    result = await flow.run("unknown", [], "adaptive")
    assert result["status"] == "insufficient"
    assert result["generations"] == 0
    web.search.assert_not_awaited()


async def test_conversation_context_and_agent_direct_route(settings):
    llm = ScriptedLLM(route="direct")
    flow, knowledge, _ = build(settings, llm)
    result = await flow.run("it?", [{"role": "user", "content": "EUV"}], "agentic")
    assert llm.calls[0][0] == "contextualize"
    assert llm.calls[1][1]["question"] == "standalone question"
    assert result["status"] == "direct"
    knowledge.search.assert_not_awaited()


async def test_explicit_web_route(settings):
    flow, knowledge, web = build(settings, ScriptedLLM(route="web"))
    result = await flow.run("today", [], "adaptive")
    knowledge.search.assert_not_awaited()
    web.search.assert_awaited_once()
    assert result["generations"] == 1
