import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.common.errors import AppError
from app.infrastructure.cache import Cache
from app.infrastructure.container import Container
from app.infrastructure.http import post_json
from app.services.llm_service import LLMService
from app.services.milvus_service import MilvusStore


async def test_agent_uses_real_function_call_schema_and_query(settings):
    settings.mode = "live"

    def handler(request):
        body = json.loads(request.content)
        assert body["tools"][0]["function"]["name"] == "rag_retriever"
        assert "response_format" not in body
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "rag_retriever",
                                        "arguments": '{"query":"EUV wavelength"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await LLMService(settings, client).call("agent", {"question": "EUV?"})
    assert result == {"route": "retrieve", "query": "EUV wavelength"}


async def test_http_errors_retry_bounded_and_never_expose_provider_body(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("app.infrastructure.http.asyncio.sleep", sleep)
    requests = []

    def timeout(request):
        requests.append(request)
        raise httpx.ReadTimeout("private-key-value")

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(AppError) as exc:
            await post_json(client, "https://test.invalid/v1", json={})
    assert len(requests) == 3
    assert "private-key-value" not in str(exc.value)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(401, text="secret"))
    ) as client:
        with pytest.raises(AppError) as exc:
            await post_json(client, "https://test.invalid/v1", json={})
        assert exc.value.status == 502
        assert "secret" not in str(exc.value)


async def test_redis_failure_falls_back_and_cache_is_bounded():
    cache = Cache("", 1)
    cache.redis = AsyncMock()
    cache.redis.get.side_effect = ConnectionError()
    cache.redis.set.side_effect = ConnectionError()
    await cache.set("one", [{"id": "source"}])
    assert await cache.get("one") == [{"id": "source"}]
    cache.redis = None
    for i in range(300):
        await cache.set(str(i), i)
    assert len(cache.local) == 256
    assert await cache.get("one") is None


def test_milvus_sdk_schema_requests_and_no_destructive_startup(settings, monkeypatch):
    pymilvus = pytest.importorskip("pymilvus")
    real_client = pymilvus.MilvusClient
    client = MagicMock()
    client.has_collection.return_value = False
    client.create_schema.side_effect = real_client.create_schema
    client.prepare_index_params.side_effect = real_client.prepare_index_params
    monkeypatch.setattr(pymilvus, "MilvusClient", lambda **kwargs: client)
    from app.common.encryption import Cipher

    cipher = Cipher(settings.encryption_key_file, create=True)
    repository = MagicMock()
    repository.ready_document_ids.return_value = ["doc"]
    store = MilvusStore(settings, repository, cipher)
    store.initialize()
    created = client.create_collection.call_args.kwargs
    schema = created["schema"].to_dict()
    assert {f["name"] for f in schema["fields"]} == {
        "id",
        "document_id",
        "text",
        "metadata",
        "dense",
        "sparse",
    }
    assert schema["functions"][0]["input_field_names"] == ["text"]
    assert "bm25_k1" in str(created["index_params"])
    client.hybrid_search.return_value = [
        [
            {
                "id": "doc_1",
                "distance": 0.03,
                "entity": {
                    "metadata": {
                        "encrypted": cipher.encrypt(
                            {"text": "evidence", "meta": {"filename": "a.md"}}, "milvus.payload:doc_1"
                        )
                    }
                },
            }
        ]
    ]
    assert store.search("EUV", [0.0] * 512, 4)[0]["text"] == "evidence"
    request = client.hybrid_search.call_args.kwargs
    assert len(request["reqs"]) == 2
    assert request["reqs"][0].anns_field == "dense"
    assert request["reqs"][1].anns_field == "sparse"
    store.upsert(
        [{"id": "doc_1", "document_id": "doc", "text": "evidence", "meta": {}, "embedding": [0.0] * 512}]
    )
    assert "sparse" not in client.upsert.call_args.kwargs["data"][0]
    assert "evidence" not in client.upsert.call_args.kwargs["data"][0]["text"]
    assert 'document_id in ["doc"]' == request["reqs"][0].expr
    store.search("EUV", [0.0] * 512, 64)
    large = client.hybrid_search.call_args.kwargs["reqs"][0]
    assert large.param["params"]["ef"] >= large.limit
    store.delete('doc"or true')
    assert json.loads(client.delete.call_args.kwargs["filter"].split(" == ")[1]) == 'doc"or true'
    client.drop_collection.assert_not_called()
    client.has_collection.side_effect = lambda name: name.endswith("_secure")
    client.describe_collection.return_value = {"fields": [{"name": "dense", "params": {"dim": 3}}]}
    with pytest.raises(AppError, match="不兼容"):
        store.initialize()
    client.drop_collection.assert_not_called()


@pytest.mark.external
@pytest.mark.skipif(not os.getenv("RAGE_TEST_MILVUS_URI"), reason="explicit test Milvus URI not configured")
async def test_real_milvus_ingest_hybrid_search_delete(settings, sample):
    settings.vector_backend = "milvus"
    settings.milvus_uri = os.environ["RAGE_TEST_MILVUS_URI"]
    settings.milvus_collection = "rag_e_test_" + uuid.uuid4().hex
    services = Container(settings)
    try:
        await services.start()
        document = await services.knowledge.ingest("chip.md", sample)
        results = await services.knowledge.search("EUV 光刻", 4)
        assert results and any("13.5" in result["text"] for result in results)
        await services.knowledge.delete(document["id"])
        assert await services.knowledge.search("EUV 光刻", 4) == []
    finally:
        try:
            if services.store.client and services.store.client.has_collection(services.store.collection):
                services.store.client.drop_collection(services.store.collection)
        finally:
            await services.close()
