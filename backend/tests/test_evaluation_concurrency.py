import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.common.context import knowledge_scope
from app.evaluation.checkpoint import Checkpoint
from app.evaluation.concurrency import DependencyFailureGuard, run_question_workers, worker_chats
from app.evaluation.datasets import Dataset
from app.evaluation.run import PairedKnowledge, make_report
from app.evaluation.timing import TimedTracing, collect_timings, install_timing
from app.infrastructure.container import Container


async def test_bounded_out_of_order_queries_commit_once_and_resume(tmp_path):
    active, maximum = 0, 0
    queries = [{"id": str(i)} for i in range(7)]
    results = {"0": {"already": True}}
    checkpoint = Checkpoint(tmp_path)
    order = []

    def factory(worker):
        async def process(index, query):
            nonlocal active, maximum
            token = knowledge_scope.set((query["id"],))
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.02 if index == 1 else 0.001)
                assert knowledge_scope.get() == (query["id"],)
                return {"worker": worker, "id": query["id"]}
            finally:
                active -= 1
                knowledge_scope.reset(token)

        return process

    def commit(query, row):
        assert query["id"] not in results
        checkpoint.put("results", query["id"], row)
        results[query["id"]] = row
        order.append(query["id"])

    await run_question_workers(queries, results, 2, factory, lambda: None, commit)
    assert maximum == 2 and order[0] == "2"
    assert set(results) == {q["id"] for q in queries}
    assert len(checkpoint.read("results")) == 6
    await run_question_workers(queries, results, 2, factory, lambda: None, commit)
    assert len(order) == 6
    checkpoint.close()


@pytest.mark.parametrize("reason", ["evaluation_stop_requested", "evaluation_dependency_unavailable"])
async def test_stop_drains_inflight_pairs_without_starting_more(reason):
    started, committed = [], []
    stop = False
    both = asyncio.Event()

    def factory(worker):
        async def process(index, query):
            nonlocal stop
            started.append(query["id"])
            if len(started) == 2:
                stop = True
                both.set()
            await both.wait()
            await asyncio.sleep(0.001)
            return {}

        return process

    def before():
        if stop:
            raise RuntimeError(reason)

    with pytest.raises(RuntimeError, match=reason):
        await run_question_workers(
            [{"id": str(i)} for i in range(9)], {}, 2, factory, before, lambda q, r: committed.append(q["id"])
        )
    assert sorted(started) == sorted(committed) == ["0", "1"]


def test_dependency_failure_guard_preserves_refusals_but_halts_repeated_outages():
    guard = DependencyFailureGuard()
    failed = {v: {"answer": {"status": "error", "error": "generation_timeout"}} for v in ("rrf", "rerank")}
    guard.observe(failed)
    guard.observe(failed)
    guard.check()
    guard.observe({v: {"answer": {"status": "insufficient"}} for v in ("rrf", "rerank")})
    assert guard.consecutive == 0
    for _ in range(3):
        guard.observe(failed)
    with pytest.raises(RuntimeError, match="evaluation_dependency_unavailable"):
        guard.check()


async def test_paired_cache_serializes_same_query_and_ranking(tmp_path):
    service = Mock()
    sources = [{"id": "x", "text": "source", "metadata": {}}]

    async def candidates(*_):
        await asyncio.sleep(0.01)
        return sources

    service.candidates = AsyncMock(side_effect=candidates)
    service.reranker.rank = AsyncMock(return_value=sources)
    paired = PairedKnowledge(service, tmp_path / "candidates", 32)
    rows = await asyncio.gather(*(paired.ranked("same") for _ in range(6)))
    assert all(row["ranked"] == sources for row in rows)
    assert service.candidates.await_count == service.reranker.rank.await_count == 1


async def test_two_question_chat_arms_are_isolated_and_share_model_gate(settings):
    settings.llm_max_concurrency = 2
    services = Container(settings)
    active, peak = 0, 0
    both_calling = asyncio.Event()
    try:
        await services.start()
        install_timing(services)
        a = await services.knowledge.ingest("a.txt", b"baseline evidence")
        b = await services.knowledge.ingest("b.txt", b"reranked evidence")

        def source(doc, text):
            return [
                {
                    "id": doc["id"] + "_0",
                    "text": text,
                    "score": 1,
                    "metadata": {"document_id": doc["id"], "base_id": "legacy", "source": text},
                }
            ]

        services.knowledge.search = AsyncMock(return_value=source(b, "Qwen"))
        baseline = SimpleNamespace(search=AsyncMock(return_value=source(a, "RRF")))

        async def llm(task, payload, observation=None):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                both_calling.set()
            await asyncio.wait_for(both_calling.wait(), timeout=20)
            await asyncio.sleep(0.01)
            active -= 1
            if task == "generate":
                return payload["original_question"] + ":" + payload["sources"][0]["text"]
            return {
                "relevance": {"relevant": True},
                "grounding": {"supported": True},
                "usefulness": {"useful": True},
            }[task]

        services.llm._call = llm
        original = services.orchestrator.knowledge
        admin = services.repository.user(username=settings.admin_username)

        async def question(text):
            chats = worker_chats(services, baseline)
            assert chats["rrf"].orchestrator.llm is services.llm
            assert chats["rerank"].orchestrator.llm is services.llm
            for variant, expected in (("rrf", "RRF"), ("rerank", "Qwen")):
                conversation = await asyncio.to_thread(
                    services.repository.create_conversation, admin["id"], text, ["legacy"]
                )
                with collect_timings() as timing:
                    result = await chats[variant].answer(conversation["id"], admin["id"], text, "adaptive")
                assert result["answer"] == text + ":" + expected
                assert timing["llm.generate"]["calls"] == 1

        await asyncio.gather(question("first"), question("second"))
        assert peak == 2
        assert services.orchestrator.knowledge is original
    finally:
        await services.close()


async def test_timing_is_context_local_and_records_failures_without_payloads():
    @asynccontextmanager
    async def span(*_, **__):
        yield None

    tracing = TimedTracing(SimpleNamespace(span=span))

    async def one(task, fail=False):
        with collect_timings() as values:
            try:
                async with tracing.span("llm", metadata={"task": task}, input="private"):
                    await asyncio.sleep(0.005)
                    if fail:
                        raise ValueError("secret")
            except ValueError:
                pass
        return values

    a, b = await asyncio.gather(one("generate"), one("grounding", True))
    assert set(a) == {"llm.generate"} and set(b) == {"llm.grounding"}
    assert b["llm.grounding"]["errors"] == 1
    assert "private" not in json.dumps([a, b]) and "secret" not in json.dumps([a, b])


def test_report_separates_old_serial_from_parallel_latency(tmp_path):
    queries = [{"id": "old", "relevant": {}}, {"id": "new", "relevant": {}}]
    rows = {}
    for query, seconds in zip(queries, [10, 30], strict=True):
        rows[query["id"]] = {
            v: {"retrieval_metrics": {}, "answer": {"seconds": seconds, "status": "answered", "metrics": {}}}
            for v in ("rrf", "rerank")
        }
    rows["new"]["execution"] = {"question_concurrency": 2}
    result = make_report(tmp_path, Dataset("test", {}, queries, {}), rows, "complete")
    groups = result["by_question_concurrency"]
    assert groups["1"]["variants"]["rrf"]["chat_latency"]["mean_seconds"] == 10
    assert groups["2"]["variants"]["rrf"]["chat_latency"]["mean_seconds"] == 30
    assert groups["1"]["variants"]["rrf"]["timed_answers"] == 0
