import json
from unittest.mock import AsyncMock, Mock

import pytest

from app.evaluation.datasets import crud_from_data, subset
from app.evaluation.metrics import (
    answer_metrics,
    answer_populations,
    paired_interval,
    retrieval_metrics,
    unique_documents,
)
from app.evaluation.run import PairedKnowledge, make_report


def test_standard_retrieval_metrics_and_empty_predictions_keep_denominator():
    pytest.importorskip("ir_measures")
    qrels = {"positive_a": 1, "positive_b": 1}
    value = retrieval_metrics(qrels, ["negative", "positive_b", "positive_b"])
    assert value["recall@1"] == 0
    assert value["recall@4"] == 0.5
    assert value["precision@4"] == 0.25
    assert value["mrr@10"] == 0.5
    assert value["hit@4"] == 1
    assert all(v == 0 for v in retrieval_metrics(qrels, []).values())
    ranking = [f"irrelevant-{i}" for i in range(10)] + ["positive_a"]
    assert retrieval_metrics(qrels, ranking)["mrr@10"] == 0


def test_answer_character_metrics_are_explicit_and_citations_removed():
    pytest.importorskip("rouge_score")
    assert answer_metrics("北京。[1]", ["北京"]) == {
        "normalized_em": 1.0,
        "character_f1": 1.0,
        "character_rougeL": 1.0,
    }
    assert all(v == 0 for v in answer_metrics("", ["北京"]).values())
    assert answer_metrics("北京是首都", ["北京"])["normalized_em"] == 0


def test_answered_only_denominator_uses_final_status_not_candidate_score():
    rows = [
        {"status": "answered", "metrics": {"character_f1": 0.8}},
        {"status": "answered", "metrics": {"character_f1": 0.4}},
        # A rejected candidate can have a high overlap; it must never re-enter the selected set.
        {"status": "insufficient", "metrics": {"character_f1": 0.99}},
        {"status": "error", "metrics": {"character_f1": 0}},
        {"status": "blocked", "metrics": {"character_f1": 0}},
    ]
    result = answer_populations(rows)
    assert result["answer_answered_only"]["character_f1"] == pytest.approx(0.6)
    population = result["answer_population"]
    assert population["answered_only_denominator"] == 2
    assert population["all_attempts_denominator"] == 5
    assert population["answer_coverage"] == 0.4
    assert population["excluded_by_status"] == {"insufficient": 1, "error": 1, "blocked": 1}
    empty = answer_populations(rows[2:])
    assert empty["answer_answered_only"] == {}  # Undefined quality, not a zero-score answered population.
    assert empty["answer_population"]["answer_coverage"] == 0
    assert answer_populations([])["answer_population"]["answer_coverage"] is None


def test_refresh_answer_scope_preserves_original_metrics_and_raw_answers(tmp_path):
    from app.evaluation.answer_scope import refresh

    rows = [
        {
            "id": str(i),
            **{
                v: {"answer": {"status": status, "metrics": {"character_f1": score}}}
                for v in ("rrf", "rerank")
            },
        }
        for i, (status, score) in enumerate((("answered", 0.8), ("insufficient", 0)))
    ]
    source = "\n".join(json.dumps(row) for row in rows)
    (tmp_path / "queries.jsonl").write_text(source, encoding="utf-8")
    original = {"character_f1": 0.4}
    metrics = {
        "status": "complete",
        "completed_queries": 2,
        "planned_queries": 2,
        "variants": {v: {"answer": original} for v in ("rrf", "rerank")},
    }
    (tmp_path / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    (tmp_path / "report.md").write_text("original scope correction", encoding="utf-8")
    report = refresh(tmp_path)
    assert report["variants"]["rerank"]["answer_answered_only"]["character_f1"] == 0.8
    assert (tmp_path / "queries.jsonl").read_text(encoding="utf-8") == source
    updated = json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))
    assert updated["variants"]["rerank"]["answer"] == original
    assert (tmp_path / "report.md").read_text(encoding="utf-8").startswith("original scope correction")
    before = (tmp_path / "metrics.json").read_bytes()
    (tmp_path / "queries.jsonl").write_text(source + "\n" + json.dumps(rows[0]), encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        refresh(tmp_path)
    assert (tmp_path / "metrics.json").read_bytes() == before


def test_complete_positive_corpus_sampling_and_dedup_rank():
    corpus = {str(i): str(i) for i in range(20)}
    queries = [{"relevant": {"1": 1, "2": 1, "3": 1}}]
    assert len(subset(corpus, queries, 1, 42)) == 3
    assert subset(corpus, queries, 8, 42) == subset(corpus, queries, 8, 42)
    docs = [{"metadata": {"document_id": key}} for key in ("a", "a", "b")]
    assert unique_documents(docs, {"a": "one", "b": "two"}) == ["one", "two"]
    with pytest.raises(ValueError):
        subset({}, queries, 8, 42)


def test_crud_uses_only_source_news_and_distinct_events():
    data = {}
    for i, task in enumerate(("questanswer_1doc", "questanswer_2docs", "questanswer_3docs"), 1):
        data[task] = [
            {
                "ID": str(event),
                "questions": f"question {event}",
                "answers": "DO NOT INDEX ANSWER",
                "thoughts": "DO NOT INDEX THOUGHTS",
                "event": "DO NOT INDEX EVENT",
                **{f"news{n}": f"article {event} {n}" for n in range(1, i + 1)},
            }
            for event in range(20)
        ]
    dataset = crud_from_data(data, 6, 20, 42)
    assert len({q["group"] for q in dataset.queries}) == 6
    assert dataset.metadata["tasks"] == dict.fromkeys(data, 2)
    assert all("DO NOT INDEX" not in value for value in dataset.corpus.values())
    assert all(set(q["relevant"]).issubset(dataset.corpus) for q in dataset.queries)


async def test_paired_adapter_uses_same_real_candidates_and_applies_top_k_after_rerank(tmp_path):
    source = [{"id": str(i), "text": str(i), "metadata": {"document_id": "d"}} for i in range(3)]
    service = Mock()
    service.candidates = AsyncMock(return_value=source)
    service.reranker.rank = AsyncMock(return_value=list(reversed(source)))
    service.repository.filter_ready = Mock(return_value={"0", "1", "2"})
    adapter = PairedKnowledge(service, tmp_path / "cache", 32)
    adapter.variant = "rrf"
    assert (await adapter.search("query", 1))[0]["id"] == "0"
    adapter.variant = "rerank"
    assert (await adapter.search("query", 1))[0]["id"] == "2"
    service.candidates.assert_awaited_once_with("query", 32)
    service.reranker.rank.assert_awaited_once_with("query", source)
    # Resuming reads the same pool; no new provider request.
    restored = PairedKnowledge(service, tmp_path / "cache", 32)
    assert (await restored.search("query", 1))[0]["id"] == "2"
    assert service.candidates.await_count == 1


def test_report_counts_failures_and_bootstrap_is_deterministic(tmp_path):
    pytest.importorskip("ir_measures")
    from app.evaluation.datasets import Dataset

    q = {"id": "q", "relevant": {"a": 1}}
    dataset = Dataset("test", {"a": "text"}, [q], {})
    result = {
        "q": {
            "id": "q",
            **{
                v: {
                    "retrieval_metrics": retrieval_metrics(q["relevant"], []),
                    "retrieval_error": "test_error",
                }
                for v in ("rrf", "rerank")
            },
        }
    }
    metrics = make_report(tmp_path, dataset, result, "complete")
    assert metrics["completed_queries"] == 1
    assert metrics["variants"]["rerank"]["retrieval_errors"] == 1
    assert json.loads((tmp_path / "metrics.json").read_text())["planned_queries"] == 1
    rows = [{"rrf": {"v": 0}, "rerank": {"v": 1}}, {"rrf": {"v": 1}, "rerank": {"v": 1}}]
    assert paired_interval(rows, "v") == paired_interval(rows, "v")


async def test_remote_protocol_disconnect_retries_without_leaking_provider_details():
    import httpx

    from app.infrastructure.http import post_json

    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.RemoteProtocolError("disconnected with private details")
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await post_json(client, "https://example.invalid/api") == {"ok": True}
    assert calls == 2


@pytest.mark.parametrize("failure", ["RemoteProtocolError", "ReadTimeout"])
async def test_usage_hook_preserves_mid_body_transport_error_for_retry(tmp_path, failure):
    import httpx

    from app.evaluation.run import Usage
    from app.infrastructure.http import post_json

    class InterruptedBody(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b'{"data":['
            raise getattr(httpx, failure)("private upstream details")

        async def aclose(self):
            self.closed = True

    stream = InterruptedBody()
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, stream=stream)
        return httpx.Response(200, json={"data": [], "usage": {"total_tokens": 12}})

    usage = Usage(tmp_path / "usage.json")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), event_hooks={"response": [usage.response]}
    ) as client:
        result = await post_json(client, "https://example.invalid/embeddings")
    assert result["data"] == []
    assert calls == 2
    assert stream.closed
    assert usage.totals["embedding"]["usage"]["total_tokens"] == 12
    assert "private upstream details" not in (tmp_path / "usage.json").read_text()


async def test_usage_accounting_failure_does_not_break_valid_response(tmp_path, monkeypatch):
    import httpx

    from app.evaluation.run import Usage
    from app.infrastructure.http import post_json

    usage = Usage(tmp_path / "usage.json")
    monkeypatch.setattr("app.evaluation.run.write_json", Mock(side_effect=OSError("disk error")))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"ok": True})),
        event_hooks={"response": [usage.response]},
    ) as client:
        assert await post_json(client, "https://example.invalid/embeddings") == {"ok": True}
