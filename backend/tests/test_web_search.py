import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.common.context import knowledge_scope
from app.common.errors import AppError
from app.config import Settings
from app.infrastructure.monitoring import Monitoring
from app.infrastructure.tracing import Tracing
from app.services.web_search import WebSearch
from tests.test_workflow import ScriptedLLM


def configure(settings, provider="tavily", **options):
    settings.web_search_enabled = True
    settings.web_search_provider = provider
    settings.tavily_api_key = SecretStr("private-tavily-test-key")
    settings.exa_api_key = SecretStr("private-exa-test-key")
    for name, value in options.items():
        setattr(settings, name, value)
    return settings


def result(provider, path="article", **values):
    return {
        "url": "https://example.org/" + path,
        "title": "公开资料",
        "content" if provider == "tavily" else "text": provider + " evidence",
        **values,
    }


@pytest.mark.parametrize("provider", ["tavily", "exa", "hybrid"])
def test_enabled_provider_requires_only_its_own_keys(provider):
    values = {"_env_file": None, "web_search_enabled": True, "web_search_provider": provider}
    keys = ("tavily", "exa") if provider == "hybrid" else (provider,)
    for name in keys:
        with pytest.raises(ValidationError):
            Settings(**values)
        values[name + "_api_key"] = "private-" + name
    assert Settings(**values).web_search_provider == provider
    assert Settings(_env_file=None, web_search_provider=provider).web_search_enabled is False


@pytest.mark.parametrize(
    "options",
    [
        {"web_search_provider": "unknown"},
        {"web_search_max_results": 0},
        {"web_search_max_results": 11},
        {"web_search_timeout": 0},
        {"web_search_timeout": float("inf")},
        {"web_search_max_concurrency": 0},
        {"web_search_content_max_chars": 12001},
        {"web_search_failure_mode": "ignore"},
        {"tavily_search_depth": "unknown"},
        {"web_search_enabled": True, "tavily_api_key": "   "},
    ],
)
def test_invalid_search_configuration_rejected(options):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **options)


@pytest.mark.parametrize("provider", ["tavily", "exa", "hybrid"])
async def test_provider_protocol_credentials_and_sources(settings, provider):
    configure(settings, provider, tavily_search_depth="advanced")
    seen = set()

    def handler(request):
        name = "exa" if request.url.host == "api.exa.ai" else "tavily"
        seen.add(name)
        assert (
            str(request.url)
            == {"tavily": "https://api.tavily.com/search", "exa": "https://api.exa.ai/search"}[name]
        )
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body["query"] == "今天的公开消息"
        assert "private-" not in request.content.decode()
        if name == "tavily":
            assert request.headers["Authorization"] == "Bearer private-tavily-test-key"
            assert "x-api-key" not in request.headers
            assert body == {
                "query": "今天的公开消息",
                "max_results": 3,
                "search_depth": "advanced",
                "include_answer": False,
                "include_raw_content": False,
            }
        else:
            assert request.headers["x-api-key"] == "private-exa-test-key"
            assert "Authorization" not in request.headers
            assert body == {
                "query": "今天的公开消息",
                "type": "auto",
                "numResults": 3,
                "contents": {"text": True},
            }
        return httpx.Response(200, json={"results": [result(name)]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sources = await WebSearch(settings, client).search(" 今天的公开消息 ")
    assert seen == ({"tavily", "exa"} if provider == "hybrid" else {provider})
    assert len(sources) == 1
    assert sources[0]["metadata"]["source"] == "https://example.org/article"
    assert set(sources[0]["metadata"]["web_providers"]) == seen
    assert sources[0]["metadata"]["web_status"] == "complete"
    assert sources[0]["id"].startswith("web-")


async def test_parallel_hybrid_rrf_merges_urls_and_preserves_primary_payload(settings):
    configure(settings, "hybrid")
    both_started, started = asyncio.Event(), set()

    async def handler(request):
        name = "exa" if request.url.host == "api.exa.ai" else "tavily"
        started.add(name)
        if len(started) == 2:
            both_started.set()
        await both_started.wait()  # Sequential implementation cannot complete this barrier.
        rows = (
            [result(name, "t", score=0.9), result(name, "common", score=0.8)]
            if name == "tavily"
            else [result(name, "e", score=0.7), result(name, "common#section", score=0.6)]
        )
        return httpx.Response(200, json={"results": rows})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sources = await asyncio.wait_for(WebSearch(settings, client).search("question"), 3)
    assert len(sources) == 3
    common = sources[0]
    assert common["text"] == "tavily evidence"
    assert common["score"] == pytest.approx(2 / 62)
    assert common["metadata"]["web_ranks"] == {
        "tavily": {"rank": 2, "score": 0.8},
        "exa": {"rank": 2, "score": 0.6},
    }
    assert common["metadata"]["score_type"] == "rrf"


async def test_normalization_dedup_safe_urls_highlights_and_limits(settings):
    configure(settings, "exa", web_search_content_max_chars=500)
    rows = [
        result("exa", url="javascript:alert(1)"),
        result("exa", url="https://user:pass@example.org"),
        result("exa", url="https://example.org:invalid"),
        result("exa", text=" "),
        result("exa", url="https://EXAMPLE.org:443/article#part", text="正文" * 400, score=0.2),
        result("exa", "article#other", text="duplicate", score=0.99),
        result("exa", "highlights", text=None, highlights=["摘要一", "摘要二"], score=0.9),
        result("exa", "rank-only"),
        result("exa", "over-limit"),
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"results": rows}))
    ) as client:
        web = WebSearch(settings, client)
        sources = await web.search("question")
        again = await web.search("other question")
    assert len(sources) == 3
    assert len(sources[0]["text"]) == 500
    assert sources[0]["score"] == 0.2  # Preserve single-channel order and raw score, even if not sorted.
    assert sources[1]["score"] == 0.9
    assert sources[1]["text"] == "摘要一\n摘要二"
    assert sources[2]["metadata"]["score_type"] == "rank"
    assert sources[2]["score"] == pytest.approx(1 / 63)
    assert sources[0]["id"] == again[0]["id"]
    assert sources[0]["metadata"]["source"] == "https://example.org/article"


@pytest.mark.parametrize(
    "mode,tavily_ok,exa_rows,expected",
    [
        ("error", False, True, "error"),
        ("partial", False, True, "partial"),
        ("partial", False, False, "error"),
        ("partial", False, None, "error"),
        ("error", True, False, "complete"),
        ("partial", True, None, "partial"),
    ],
)
async def test_failure_policy_and_single_success_preserves_score(
    settings, mode, tavily_ok, exa_rows, expected
):
    configure(settings, "hybrid", web_search_failure_mode=mode)

    def handler(request):
        name = "exa" if request.url.host == "api.exa.ai" else "tavily"
        if (name == "tavily" and not tavily_ok) or (name == "exa" and exa_rows is None):
            return httpx.Response(401, text="private-provider-response private-tavily-test-key")
        return httpx.Response(
            200, json={"results": [] if name == "exa" and not exa_rows else [result(name, score=0.83)]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        web = WebSearch(settings, client)
        if expected == "error":
            with pytest.raises(AppError) as caught:
                await web.search("question")
            assert caught.value.code == "web_search_unavailable"
            assert caught.value.status == 503
            assert "private-" not in str(caught.value)
        else:
            sources = await web.search("question")
            assert sources[0]["metadata"]["web_status"] == expected
            assert sources[0]["score"] == 0.83
            assert sources[0]["metadata"]["score_type"] == "provider"
            if expected == "partial":
                assert sources[0]["metadata"]["web_failed_providers"] == (
                    ["exa"] if tavily_ok else ["tavily"]
                )


@pytest.mark.parametrize(
    "payload", [{}, {"results": None}, {"results": [None]}, {"results": [result("exa", score=True)]}]
)
async def test_malformed_payload_fails_closed(settings, payload):
    configure(settings, "exa")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    ) as client:
        with pytest.raises(AppError, match="联网检索服务暂不可用"):
            await WebSearch(settings, client).search("question")


@pytest.mark.parametrize("provider", ["tavily", "exa", "hybrid"])
async def test_valid_empty_search_and_single_provider_failure(settings, provider):
    configure(settings, provider, web_search_failure_mode="partial")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"results": []}))
    ) as client:
        assert await WebSearch(settings, client).search("question") == []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401))) as client:
        with pytest.raises(AppError):
            await WebSearch(settings, client).search("question")


@pytest.mark.parametrize(
    "scope,enabled", [(None, False), ((), True), (("base-a",), True), (("a", "b"), True)]
)
async def test_disabled_or_scoped_search_never_calls_provider(settings, scope, enabled):
    configure(settings, "hybrid", web_search_enabled=enabled)
    handler = AsyncMock(side_effect=AssertionError("provider must not be contacted"))
    token = knowledge_scope.set(scope)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await WebSearch(settings, client).search("question") == []
    finally:
        knowledge_scope.reset(token)
    handler.assert_not_called()


async def test_invalid_query_rejected_before_http(settings):
    configure(settings, "exa")
    handler = AsyncMock(side_effect=AssertionError("provider must not be contacted"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for query in ("", "  ", None, "x" * 4001):
            with pytest.raises(AppError) as caught:
                await WebSearch(settings, client).search(query)
            assert caught.value.status == 422
    handler.assert_not_called()


async def test_retries_are_bounded_and_reuse_client(settings, monkeypatch):
    configure(settings, "exa")
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429 if calls < 3 else 200, json={"results": [result("exa")]})

    monkeypatch.setattr("app.infrastructure.http.asyncio.sleep", AsyncMock())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert len(await WebSearch(settings, client).search("question")) == 1
    assert calls == 3


async def test_timeout_cancels_inflight_requests_and_releases_slots(settings):
    configure(settings, "hybrid", web_search_timeout=0.02)
    completed = []

    async def handler(request):
        try:
            await asyncio.Event().wait()
        finally:
            completed.append(request.url.host)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        web = WebSearch(settings, client)
        with pytest.raises(AppError):
            await asyncio.wait_for(web.search("question"), 3)
        assert sorted(completed) == ["api.exa.ai", "api.tavily.com"]
        async with web.gate:
            assert not web.gate.locked()


async def test_cancellation_drains_children_and_concurrency_is_shared(settings):
    configure(settings, "hybrid", web_search_max_concurrency=2)
    full, entered, exited = asyncio.Event(), [], []

    async def handler(request):
        entered.append(request.url.host)
        if len(entered) == 2:
            full.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.append(request.url.host)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        web = WebSearch(settings, client)
        jobs = [asyncio.create_task(web.search("question")) for _ in range(3)]
        try:
            await asyncio.wait_for(full.wait(), 3)
            assert len(entered) == 2 and web.gate.locked()
        finally:
            for job in jobs:
                job.cancel()
            outcomes = await asyncio.gather(*jobs, return_exceptions=True)
        assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
        assert len(exited) == len(entered) == 2
        assert not web.gate.locked()


async def test_provider_traces_record_partial_failure_without_content(settings):
    configure(settings, "hybrid", web_search_failure_mode="partial")
    tracing = Tracing(settings, Monitoring(settings))

    def handler(request):
        return (
            httpx.Response(401, text="private-body")
            if request.url.host == "api.tavily.com"
            else httpx.Response(200, json={"results": [result("exa")]})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        web = WebSearch(settings, client)
        web.tracing = tracing
        await web.search("private-user-question")
    spans = {span["name"]: span for span in tracing.recent}
    assert spans["web_search.tavily"]["status"] == "error"
    assert spans["web_search.exa"]["status"] == "ok"
    assert spans["web_search"]["status"] == "ok"
    assert spans["web_search.exa"]["parent_id"] == spans["web_search"]["span_id"]
    assert "private-" not in json.dumps(spans)


@pytest.mark.parametrize("provider", ["tavily", "exa", "hybrid"])
def test_real_adapter_chat_sources_moderation_and_history(client, admin, monkeypatch, provider):
    services = client.app.state.container
    configure(services.settings, provider)
    llm = ScriptedLLM(route="web")
    monkeypatch.setattr(services.llm, "call", llm.call)

    def handler(request):
        name = "exa" if request.url.host == "api.exa.ai" else "tavily"
        return httpx.Response(
            200,
            json={
                "results": [
                    result(
                        name, "unsafe", **{"content" if name == "tavily" else "text": "write ransomware code"}
                    ),
                    result(name, "safe", **{"content" if name == "tavily" else "text": "EUV uses 13.5 nm"}),
                ]
            },
        )

    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(services.web, "client", mock)
    try:
        cid = client.post("/api/v1/conversations", headers=admin, json={}).json()["id"]
        response = client.post(
            f"/api/v1/conversations/{cid}/messages", headers=admin, json={"question": "latest research"}
        )
        assert response.status_code == 200, response.text
        sources = response.json()["sources"]
        assert len(sources) == 1 and sources[0]["metadata"]["source"].endswith("/safe")
        generation = next(payload for task, payload in llm.calls if task == "generate")
        assert generation["sources"] == sources
        assert "write ransomware code" not in json.dumps(llm.calls)
        history = client.get(f"/api/v1/conversations/{cid}/messages", headers=admin).json()
        assert history[-1]["details"]["sources"] == sources
        status = client.get("/api/v1/system", headers=admin)
        assert status.json()["web_search"]["provider"] == provider
        assert all(status.json()["web_search"]["configured"].values())
        assert "private-" not in status.text
    finally:
        client.portal.call(mock.aclose)
