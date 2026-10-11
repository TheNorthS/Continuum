import asyncio
import json
import os
import threading
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.common.context import knowledge_scope
from app.common.errors import AppError
from app.config import Settings
from app.infrastructure.cache import Cache
from app.infrastructure.container import Container
from app.services.rerank_service import Reranker


def sources(count=3):
    return [
        {
            "id": f"doc_{i}",
            "text": f"candidate {i}",
            "score": 1 / (60 + i),
            "metadata": {"document_id": "doc", "filename": "source.md"},
        }
        for i in range(count)
    ]


def api_settings(settings):
    settings.rerank_provider = "api"
    settings.rerank_api_url = "https://rerank.example/v1/rerank"
    settings.rerank_api_key = SecretStr("rerank-secret")
    settings.rerank_model = "dedicated-model"
    return settings


async def test_online_qwen3_uses_flat_payload_and_independent_api_key(settings):
    api_settings(settings)
    settings.rerank_api_format = "qwen3"
    settings.rerank_model = "qwen3-rerank"
    settings.rerank_api_url = "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-api/v1/reranks"

    def handler(request):
        assert str(request.url) == settings.rerank_api_url
        assert request.headers["Authorization"] == "Bearer rerank-secret"
        assert json.loads(request.content) == {
            "model": "qwen3-rerank",
            "query": "query",
            "documents": ["candidate 0", "candidate 1"],
            "top_n": 2,
        }
        return httpx.Response(
            200,
            json={
                "object": "list",
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.1},
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ranker = Reranker(settings, client, Cache("", 300))
        ranker._predict_local = Mock(side_effect=AssertionError("No local models for online Qwen"))
        ranked = await ranker.rank("query", sources(2))
        assert [r["id"] for r in ranked] == ["doc_1", "doc_0"]
        ranker._predict_local.assert_not_called()


async def test_api_reranks_by_index_preserves_original_sources_and_uses_cache(settings):
    api_settings(settings)
    calls = []

    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert body == {
            "model": "dedicated-model",
            "query": "query",
            "documents": ["candidate 0", "candidate 1", "candidate 2"],
            "top_n": 3,
            "return_documents": False,
        }
        assert request.headers["Authorization"] == "Bearer rerank-secret"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 2, "relevance_score": 0.9, "document": {"text": "provider injection"}},
                    {"index": 0, "relevance_score": 0.1},
                    {"index": 1, "relevance_score": 0.9},
                ]
            },
        )

    cache = Cache("", 300)
    original = sources()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ranker = Reranker(settings, client, cache)
        result = await ranker.rank("query", original)
        assert [r["id"] for r in result] == ["doc_1", "doc_2", "doc_0"]  # Stable ties.
        assert result[0]["text"] == "candidate 1" and result[0]["score"] == original[1]["score"]
        assert result[0]["rerank_score"] == 0.9
        assert "rerank_score" not in original[0]
        assert result == await ranker.rank("query", original)
        assert len(calls) == 1
        settings.rerank_min_score = 0.9
        assert len(await ranker.rank("query", original)) == 2
        assert len(calls) == 1  # Cached scores may be re-filtered with another threshold.
        settings.rerank_revision = "changed"
        await ranker.rank("query", original)
        assert len(calls) == 2
        assert "secret" not in repr(ranker.status())


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"index": 0, "relevance_score": 0.1}],
        [{"index": 0, "relevance_score": 0.1}] * 3,
        [
            {"index": 0, "relevance_score": 0.1},
            {"index": 1, "relevance_score": 0.2},
            {"index": 3, "relevance_score": 0.9},
        ],
        [
            {"index": False, "relevance_score": 0.1},
            {"index": 1, "relevance_score": 0.2},
            {"index": 2, "relevance_score": 0.3},
        ],
        [{"index": i, "relevance_score": "0.9"} for i in range(3)],
        [{"index": i, "relevance_score": True} for i in range(3)],
        [{"index": i, "relevance_score": -0.1} for i in range(3)],
        [{"index": i, "relevance_score": 1.1} for i in range(3)],
        None,
    ],
)
async def test_malformed_api_scores_fail_closed(settings, rows):
    api_settings(settings)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"results": rows}))
    ) as client:
        ranker = Reranker(settings, client, Cache("", 300))
        with pytest.raises(AppError) as exc:
            await ranker.rank("query", sources())
        assert exc.value.code == "rerank_unavailable"
        assert ranker.status()["state"] == "error"


@pytest.mark.parametrize("values", [[float("nan")], [float("inf")], [[0.5]], [True]])
def test_non_finite_or_multiclass_local_scores_rejected(values):
    with pytest.raises(AppError):
        Reranker.validate_scores(values, 1)


async def test_failure_fallback_is_explicit_never_cached_and_cancellation_propagates(settings):
    api_settings(settings)
    settings.rerank_failure_mode = "rrf"
    async with httpx.AsyncClient() as client:
        ranker = Reranker(settings, client, Cache("", 300))
        ranker._api = AsyncMock(side_effect=RuntimeError("sensitive upstream data"))
        original = sources()
        result = await ranker.rank("query", original)
        assert [r["id"] for r in result] == [r["id"] for r in original]
        assert all(r["metadata"]["rerank_status"] == "fallback" and "rerank_score" not in r for r in result)
        assert "sensitive" not in repr(result) and not ranker.cache.local
        ranker._api.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await ranker.rank("query", original)


async def test_timeout_covers_queue_and_api_work(settings):
    api_settings(settings)
    settings.rerank_timeout = 0.02
    async with httpx.AsyncClient() as client:
        ranker = Reranker(settings, client, Cache("", 300))

        async def slow(*args):
            await asyncio.sleep(5)

        ranker._api = slow
        with pytest.raises(AppError) as exc:
            await ranker.rank("query", sources())
        assert exc.value.code == "rerank_timeout"


async def test_api_concurrency_bounded_and_cache_separates_candidate_sets(settings):
    api_settings(settings)
    settings.rerank_max_concurrency = 2
    active, maximum, calls = 0, 0, 0
    async with httpx.AsyncClient() as client:
        ranker = Reranker(settings, client, Cache("", 300))

        async def score(query, docs):
            nonlocal active, maximum, calls
            active += 1
            calls += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.01)
                return [0.5] * len(docs)
            finally:
                active -= 1

        ranker._api = score
        await asyncio.gather(*(ranker.rank(str(i), sources()) for i in range(7)))
        assert maximum == 2
        before = calls
        await ranker.rank("0", sources(1))
        modified = sources(1)
        modified[0]["text"] = "edited document"
        await ranker.rank("0", modified)
        assert calls == before + 2


async def test_local_cancel_keeps_model_permit_until_native_worker_finishes(settings):
    settings.rerank_provider = "huggingface"
    started, release = threading.Event(), threading.Event()
    calls = 0

    def predict(*args):
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=3)
        return [0.5] * 3

    async with httpx.AsyncClient() as client:
        ranker = Reranker(settings, client, Cache("", 300))
        ranker._predict_local = predict
        first = asyncio.create_task(ranker.rank("first", sources()))
        assert await asyncio.to_thread(started.wait, 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(ranker.rank("second", sources()))
        try:
            await asyncio.sleep(0.03)
            assert calls == 1
        finally:
            release.set()
        assert len(await second) == 3 and calls == 2
        await ranker.close()


async def test_candidate_pool_precedes_top_k_acl_is_checked_before_and_after(settings):
    settings.rerank_provider = "api"
    settings.rerank_candidates = 4
    services = Container(settings)
    try:
        source = sources(4)
        services.repository.revision = Mock(return_value="rev")
        services.store.search = Mock(return_value=source)
        services.repository.filter_ready = Mock(side_effect=[{"doc_0", "doc_2", "doc_3"}, {"doc_0", "doc_2"}])
        services.reranker._api = AsyncMock(return_value=[0.1, 0.8, 0.9])
        result = await services.knowledge.search("query", 1)
        assert services.store.search.call_args.args[-1] == 8
        assert services.reranker._api.call_args.args[1] == ["candidate 0", "candidate 2", "candidate 3"]
        assert result[0]["id"] == "doc_2"  # Initial rank 3, rescued by model; top ranked doc_3 revoked.
        assert result[0]["rerank_score"] == 0.8
        assert services.monitoring.snapshot()["rerank"]["count"] == 1
        assert any(span["name"] == "rerank" for span in services.tracing.recent)
    finally:
        await services.close()


def test_http_single_multi_library_and_final_chat_use_reranker(client, admin, monkeypatch):
    from tests.test_knowledge_scope import base, create, document

    a, b = base(client, admin, "库 A"), base(client, admin, "库 B")
    da, db = document(client, admin, a, "甲"), document(client, admin, b, "乙")
    services = client.app.state.container
    services.settings.rerank_provider = "api"
    seen = []

    async def score(query, documents):
        seen.append((knowledge_scope.get(), documents))
        return [0.8 if "乙" in text else 0.2 for text in documents]

    monkeypatch.setattr(services.reranker, "_api", score)
    for ids, expected in (([a], da), ([b], db), ([a, b], db)):
        response = client.post(
            "/api/v1/knowledge/search", headers=admin, json={"query": "EUV", "top_k": 1, "base_ids": ids}
        )
        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["metadata"]["document_id"] == expected
        assert "rerank_score" in response.json()["results"][0]
    assert len(seen[0][1]) == 1 and "乙" not in seen[0][1][0]
    cid = create(client, admin, [a, b])
    response = client.post(
        f"/api/v1/conversations/{cid}/messages", headers=admin, json={"question": "EUV 波长"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["sources"][0]["metadata"]["document_id"] == db
    assert all(s["metadata"]["rerank_status"] == "ranked" for s in response.json()["sources"])
    assert client.get("/api/v1/system", headers=admin).json()["reranker"]["enabled"]
    assert "rerank" in client.get("/api/v1/admin/observability", headers=admin).json()["profiler"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rerank_provider": "api"},
        {"rerank_provider": "api", "rerank_api_url": "https://example.com/rerank"},
        {
            "rerank_provider": "api",
            "rerank_api_url": "https://secret@example.com/rerank",
            "rerank_api_key": "key",
        },
        {"rerank_provider": "huggingface", "rerank_model": " "},
        {"rerank_min_score": float("nan")},
        {"rerank_candidates": 101},
    ],
)
def test_rerank_configuration_validation(kwargs):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **kwargs)


async def test_disabled_empty_and_threshold_remove_all_without_fake_scores(settings):
    async with httpx.AsyncClient() as client:
        ranker = Reranker(settings, client, Cache("", 300))
        ranker._api = AsyncMock(return_value=[0.1, 0.2, 0.3])
        original = sources()
        assert await ranker.rank("query", original) is original
        ranker._api.assert_not_awaited()
        settings.rerank_provider = "api"
        assert await ranker.rank("query", []) == []
        settings.rerank_min_score = 0.5
        assert await ranker.rank("query", original) == []


async def test_graph_candidates_enter_reranker_after_dedup(settings):
    settings.rerank_provider = "api"
    settings.rerank_candidates = 4
    services = Container(settings)
    try:
        docs = sources(3)
        services.repository.revision = Mock(return_value="rev")
        services.repository.filter_ready = Mock(return_value={s["id"] for s in docs})
        services.store.search = Mock(return_value=docs[:2])
        services.knowledge.graph = Mock(enabled=True, recall=AsyncMock(return_value=docs[1:]))
        services.reranker._api = AsyncMock(
            side_effect=lambda query, texts: [0.9 if text == "candidate 2" else 0.1 for text in texts]
        )
        result = await services.knowledge.search("query", 1)
        assert result[0]["id"] == "doc_2"
        assert len(services.reranker._api.call_args.args[1]) == 3
        services.knowledge.graph.recall.assert_awaited_once_with("query", 4)
    finally:
        await services.close()


@pytest.mark.external
@pytest.mark.skipif(not os.getenv("RAGE_TEST_RERANK_MODEL"), reason="local reranker model not configured")
async def test_real_local_cross_encoder_promotes_relevant_chinese_passage(settings):
    settings.rerank_provider = "huggingface"
    settings.rerank_model = os.environ["RAGE_TEST_RERANK_MODEL"]
    settings.rerank_timeout = 120
    async with httpx.AsyncClient() as client:
        ranker = Reranker(settings, client, Cache("", 300))
        try:
            docs = sources(3)
            for source, text in zip(
                docs,
                ["香蕉是一种水果。", "PostgreSQL 是关系数据库。", "EUV 光刻使用 13.5 纳米波长的极紫外光。"],
                strict=True,
            ):
                source["text"] = text
            result = await ranker.rank("EUV 光刻的波长是多少？", docs)
            assert result[0]["id"] == "doc_2"
            assert result[0]["rerank_score"] > result[-1]["rerank_score"]
            assert ranker.status()["state"] == "ready"
            single = await ranker.rank("EUV 光刻的波长是多少？", [docs[2]])
            assert len(single) == 1 and single[0]["rerank_score"] > 0
        finally:
            await ranker.close()
