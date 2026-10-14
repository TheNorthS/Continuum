import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.common.errors import AppError
from app.services.llm_service import LLMService
from tests.test_workflow import SOURCE, ScriptedLLM, build

ISSUES = [{"claim": "EUV 使用 193 纳米波长", "reason": "来源[1]给出 13.5 纳米，193 纳米与该来源矛盾。"}]
PREVIOUS = "EUV 使用 193 纳米波长 [1]。"
CORRECTED = "EUV 使用 13.5 纳米波长 [1]。"


@pytest.mark.parametrize("correct_second", [True, False])
async def test_live_workflow_passes_feedback_and_previous_answer_then_rechecks(settings, correct_second):
    settings.mode = "live"
    generated, grades, useful, moderation_calls, events = [], [], [], [], []

    def provider(request):
        body = json.loads(request.content)
        payload = json.loads(body["messages"][-1]["content"])
        if "knowledge_domain" in payload:
            answer = {"route": "retrieve"}
        elif "document" in payload:
            answer = {"relevant": True}
        elif "sources" in payload and "answer" in payload:
            grades.append(payload)
            supported = correct_second and len(grades) > 1
            answer = {"supported": supported, "issues": [] if supported else ISSUES}
            assert body["response_format"] == {"type": "json_object"}
        elif "answer" in payload:
            useful.append(payload)
            answer = {"useful": True}
        else:
            generated.append(payload)
            if len(generated) == 2:
                assert (json.dumps(ISSUES, ensure_ascii=False), "output") in moderation_calls
            answer = CORRECTED if len(generated) > 1 else PREVIOUS
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": answer if isinstance(answer, str) else json.dumps(answer)}}
                ]
            },
        )

    async def guard(text, stage):
        moderation_calls.append((text, stage))

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        flow, knowledge, _ = build(settings, LLMService(settings, client))
        flow.moderation = AsyncMock()
        flow.moderation.filter_sources.return_value = [SOURCE]
        flow.moderation.guard.side_effect = guard
        result = await flow.run("EUV 波长是多少？", [], "adaptive", AsyncMock(side_effect=events.append))

    assert len(generated) == len(grades) == result["generations"] == 2
    assert "previous_answer" not in generated[0] and "grounding_feedback" not in generated[0]
    assert generated[1] == {**generated[0], "previous_answer": PREVIOUS, "grounding_feedback": ISSUES}
    assert grades[0]["answer"] == PREVIOUS and grades[1]["answer"] == CORRECTED
    assert len(useful) == int(correct_second)  # No usefulness judge for an ungrounded answer.
    knowledge.search.assert_awaited_once()
    assert result["status"] == ("answered" if correct_second else "insufficient")
    if correct_second:
        assert result["answer"] == CORRECTED
    else:
        assert not result["sources"] and CORRECTED not in result["answer"]
    exported = json.dumps({"result": result, "events": events}, ensure_ascii=False)
    assert PREVIOUS not in exported and ISSUES[0]["reason"] not in exported
    assert "grounding_feedback" not in exported


@pytest.mark.parametrize(
    "verdict",
    [
        {"supported": True, "issues": []},
        {"supported": False, "issues": ISSUES},
    ],
)
async def test_live_grounding_protocol_returns_validated_feedback(settings, verdict):
    settings.mode = "live"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(verdict)}}]})
        )
    ) as client:
        assert (
            await LLMService(settings, client).call("grounding", {"sources": [SOURCE], "answer": PREVIOUS})
            == verdict
        )


@pytest.mark.parametrize(
    "verdict",
    [
        {"supported": False},
        {"supported": False, "issues": []},
        {"supported": True, "issues": ISSUES},
        {"supported": "false", "issues": ISSUES},
        {"supported": 1, "issues": []},
        {"supported": False, "issues": [{"claim": "x", "reason": "  "}]},
        {"supported": False, "issues": [{"claim": 123, "reason": "why"}]},
        {"supported": False, "issues": [{"claim": "x" * 401, "reason": "why"}]},
        {"supported": False, "issues": [{"claim": "x", "reason": "x" * 601}]},
        {"supported": False, "issues": ISSUES * 5},
        {"supported": False, "issues": [{**ISSUES[0], "instruction": "private-extra"}]},
        {"supported": False, "issues": ISSUES, "extra": "private-extra"},
        {"supported": False, "issues": "private-invalid"},
        [False, "private-invalid"],
    ],
)
async def test_invalid_grounding_is_an_error_not_a_blind_retry(settings, verdict):
    settings.mode = "live"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(verdict)}}]})
        )
    ) as client:
        with pytest.raises(AppError) as caught:
            await LLMService(settings, client).call("grounding", {"sources": [SOURCE], "answer": PREVIOUS})
    assert caught.value.code == "llm_format" and caught.value.status == 502
    assert "private-" not in str(caught.value)


@pytest.mark.parametrize("node", ["rewrite", "retrieve", "web"])
async def test_changed_evidence_clears_stale_feedback(settings, node):
    llm = ScriptedLLM()
    flow, _, _ = build(settings, llm)
    state = {
        "query": "old question",
        "original_question": "question",
        "history": [],
        "answer": PREVIOUS,
        "sources": [SOURCE],
        "grounding_feedback": ISSUES,
        "rewrites": 0,
        "generations": 1,
    }
    state.update(await getattr(flow, node)(state))
    assert state["grounding_feedback"] == []
    await flow.generate(state)
    payload = next(p for task, p in llm.calls if task == "generate")
    assert "grounding_feedback" not in payload and "previous_answer" not in payload


async def test_each_turn_starts_without_feedback_and_success_clears_it(settings):
    llm = ScriptedLLM(supported=False)
    flow, _, _ = build(settings, llm)
    await flow.run("first", [], "adaptive")
    llm.supported = True
    llm.calls.clear()
    result = await flow.run("second", [], "adaptive")
    generated = [p for task, p in llm.calls if task == "generate"]
    assert len(generated) == 1 and result["generations"] == 1
    assert "grounding_feedback" not in generated[0]
    state = await flow.grade_answer(
        {"sources": [SOURCE], "answer": CORRECTED, "original_question": "q", "grounding_feedback": ISSUES}
    )
    assert state["grounding_feedback"] == []


@pytest.mark.parametrize("stream", [False, True])
def test_feedback_and_rejected_answer_never_reach_api_sse_or_history(client, admin, monkeypatch, stream):
    services = client.app.state.container
    llm = ScriptedLLM(route="web")
    count = 0

    async def call(task, payload):
        nonlocal count
        if task == "generate":
            count += 1
            if count == 2:
                assert payload["grounding_feedback"] == ISSUES
                assert payload["previous_answer"] == PREVIOUS
            return PREVIOUS if count == 1 else CORRECTED
        if task == "grounding":
            return {"supported": count > 1, "issues": [] if count > 1 else ISSUES}
        return await llm.call(task, payload)

    monkeypatch.setattr(services.llm, "call", call)
    monkeypatch.setattr(services.web, "search", AsyncMock(return_value=[{**SOURCE, "id": "web-euv"}]))
    cid = client.post("/api/v1/conversations", headers=admin, json={}).json()["id"]
    endpoint = "stream" if stream else "messages"
    response = client.post(
        f"/api/v1/conversations/{cid}/{endpoint}", headers=admin, json={"question": "EUV 波长是多少？"}
    )
    assert response.status_code == 200, response.text
    assert count == 2
    if stream:
        assert "event: result" in response.text
    history = client.get(f"/api/v1/conversations/{cid}/messages", headers=admin).json()
    assert len(history) == 2 and history[-1]["content"] == CORRECTED
    for text in (response.text, json.dumps(history, ensure_ascii=False)):
        assert PREVIOUS not in text and ISSUES[0]["reason"] not in text
        assert "grounding_feedback" not in text and "previous_answer" not in text
