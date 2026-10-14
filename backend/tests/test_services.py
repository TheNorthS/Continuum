import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from app.common.errors import AppError
from app.config import Settings
from app.infrastructure.container import Container
from app.knowledge.rag.document_processor import DocumentProcessor, sections
from app.knowledge.rag.embedding import Embedder
from app.services.llm_service import LLMService
from app.services.web_search import WebSearch
from app.utils.security import decode_token, hash_password, issue_token, verify_password


def test_password_jwt_and_production_config():
    hashed = hash_password("example-pass")
    assert verify_password("example-pass", hashed)
    assert not verify_password("wrong", hashed)
    token = issue_token("user-id", "x" * 32, 10)
    assert decode_token(token, "x" * 32) == "user-id"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mode="live")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, chunk_size=200, chunk_overlap=200)


async def test_heading_code_boundaries_and_semantic_chunking(settings):
    text = "# Parent\n## Child\n正文\n```python\n# code comment\n```\n更多正文\n## Next\n第二段"
    parsed = sections(text, "a.md")
    assert parsed[0][0] == "Parent > Child"
    assert "# code comment" in parsed[0][2]
    assert parsed[1][0] == "Parent > Next"
    settings.chunk_size = 200
    settings.chunk_overlap = 20
    settings.semantic_chunking = True
    async with httpx.AsyncClient() as client:
        processor = DocumentProcessor(settings, Embedder(settings, client))
        chunks = await processor.parse("long.md", ("# Header\n" + "光刻技术。" * 400).encode())
    assert len(chunks) > 1
    assert all(len(c["text"]) <= 200 for c in chunks)
    assert all(c["meta"]["title"] == "Header" for c in chunks)


async def test_semantic_explicit_token_limit_falls_back_without_truncating(settings):
    settings.semantic_chunking = True
    settings.chunk_size, settings.chunk_overlap = 1500, 150
    text = "引言。" + "长句内容" * 3300
    request = httpx.Request("POST", "https://example.invalid/embeddings")
    response = httpx.Response(
        400,
        request=request,
        json={"error": {"message": "Invalid 'input[1]': maximum input length is 8192 tokens."}},
    )
    failure = AppError("upstream_error", "redacted", 502)
    failure.__cause__ = httpx.HTTPStatusError("redacted", request=request, response=response)
    processor = DocumentProcessor(settings, AsyncMock(embed=AsyncMock(side_effect=failure)))
    chunks = await processor.parse("long.txt", text.encode())
    assert [chunk["text"] for chunk in chunks] == list(processor._bounded(text))
    assert all(len(chunk["text"]) <= 1500 for chunk in chunks)
    restored = chunks[0]["text"] + "".join(chunk["text"][150:] for chunk in chunks[1:])
    assert restored == text


@pytest.mark.parametrize(
    "status,message",
    [
        (401, "invalid key"),
        (429, "quota exceeded"),
        (400, "invalid model"),
        (503, "maximum input length is 8192 tokens."),
    ],
)
async def test_semantic_other_provider_failures_do_not_fallback(settings, status, message):
    settings.semantic_chunking = True
    request = httpx.Request("POST", "https://example.invalid/embeddings")
    response = httpx.Response(status, request=request, json={"error": {"message": message}})
    failure = AppError("upstream_error", "redacted", 502)
    failure.__cause__ = httpx.HTTPStatusError("redacted", request=request, response=response)
    processor = DocumentProcessor(settings, AsyncMock(embed=AsyncMock(side_effect=failure)))
    with pytest.raises(AppError) as caught:
        await processor.parse("long.txt", ("引言。" + "正文" * 4000).encode())
    assert caught.value is failure


async def test_provider_contract_retries_and_strict_json(settings, monkeypatch):
    settings.mode = "live"
    settings.llm_model = "provider-model"
    calls = []

    async def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"relevant":true}'}}]})

    monkeypatch.setattr("app.infrastructure.http.asyncio.sleep", AsyncMock())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        llm = LLMService(settings, client)
        assert (await llm.call("relevance", {"question": "EUV", "document": "13.5"}))["relevant"]
        assert len(calls) == 2
        assert calls[-1].url.path == "/v1/chat/completions"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": '{"relevant":"yes"}'}}]}
            )
        )
    ) as client:
        with pytest.raises(AppError, match="模型输出格式"):
            await LLMService(settings, client).call("relevance", {})


async def test_embedding_dimensions_and_web_sources(settings):
    settings.embedding_provider = "api"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})
        )
    ) as client:
        with pytest.raises(AppError, match="维度"):
            await Embedder(settings, client).embed(["EUV"])
    settings.web_search_enabled = True
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "results": [
                        {"content": "web evidence", "title": "Article", "url": "https://example.org/article"}
                    ]
                },
            )
        )
    ) as client:
        sources = await WebSearch(settings, client).search("query")
        assert sources[0]["metadata"]["source"] == "https://example.org/article"
        assert sources[0]["text"] == "web evidence"


async def test_partial_index_failure_hidden_and_recoverable(settings, sample, monkeypatch):
    services = Container(settings)
    try:
        await services.start()

        def fail(_):
            raise RuntimeError("partial insert")

        monkeypatch.setattr(services.store, "upsert", fail)
        with pytest.raises(RuntimeError):
            await services.knowledge.ingest("test.md", sample)
        documents = services.repository.documents()
        assert documents[0]["status"] == "failed"
        assert services.repository.ready_chunks() == []
        await services.knowledge.delete(documents[0]["id"])
        assert services.repository.documents() == []
    finally:
        await services.close()


async def test_chat_timeout_leaves_no_partial_history(settings, monkeypatch):
    settings.request_timeout = 0.01
    services = Container(settings)
    try:
        await services.start()
        user = services.repository.user(username="admin")
        conversation = services.repository.create_conversation(user["id"], "timeout")

        async def slow(*args):
            await asyncio.sleep(5)

        monkeypatch.setattr(services.orchestrator, "run", slow)
        with pytest.raises(AppError, match="超时"):
            await services.chat.answer(conversation["id"], user["id"], "test", "adaptive")
        assert services.repository.history(conversation["id"], user["id"]) == []
        token = services.repository.claim(conversation["id"], user["id"])
        services.repository.release(conversation["id"], token)
    finally:
        await services.close()


async def test_embedding_model_change_requires_explicit_reindex(settings, sample):
    first = Container(settings)
    try:
        await first.start()
        await first.knowledge.ingest("chip.md", sample)
    finally:
        await first.close()
    settings.embedding_model = "another-model-with-the-same-dimension"
    second = Container(settings)
    try:
        with pytest.raises(AppError, match="配置已变更"):
            await second.start()
    finally:
        await second.close()


async def test_live_pipeline_with_provider_protocol(settings, sample):
    # Exercise all live adapters without consuming a user's paid API or uploading their documents.
    settings.mode = "live"
    settings.embedding_provider = "api"
    settings.llm_model = "test-model"
    tasks = []

    def provider(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": i, "embedding": [1.0] + [0.0] * (settings.embedding_dimension - 1)}
                        for i in range(len(body["input"]))
                    ]
                },
            )
        payload = json.loads(body["messages"][-1]["content"])
        if "stage" in payload:
            from app.safety.policy import CATEGORIES

            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps(dict.fromkeys(CATEGORIES, 0.0))},
                        }
                    ]
                },
            )
        if "knowledge_domain" in payload:
            tasks.append("route")
            content = '{"route":"retrieve"}'
        elif "document" in payload:
            tasks.append("relevance")
            content = '{"relevant":true}'
        elif "sources" in payload and "answer" in payload:
            tasks.append("grounding")
            content = '{"supported":true,"issues":[]}'
        elif "answer" in payload:
            tasks.append("usefulness")
            content = '{"useful":true}'
        else:
            tasks.append("generate")
            content = "EUV 光刻使用 13.5 纳米波长 [1]。"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    services = Container(settings)
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as mock:
        services.embedder.client = mock
        services.llm.client = mock
        await services.moderation.http.aclose()
        services.moderation.http = mock
        try:
            await services.start()
            await services.knowledge.ingest("live.md", sample)
            user = services.repository.user(username="admin")
            conversation = services.repository.create_conversation(user["id"], "live test")
            result = await services.chat.answer(conversation["id"], user["id"], "EUV 的波长？", "adaptive")
            assert result["runtime"] == "live" and result["status"] == "answered"
            assert result["sources"] and "13.5" in result["answer"]
            assert {"route", "relevance", "generate", "grounding", "usefulness"}.issubset(tasks)
        finally:
            await services.close()
