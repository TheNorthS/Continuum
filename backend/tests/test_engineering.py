import asyncio
import gzip
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock

import httpx
import pytest

from app.infrastructure.cache import Cache
from app.infrastructure.container import Container
from app.infrastructure.monitoring import Monitoring
from app.infrastructure.tracing import CURRENT_SPAN, Tracing
from app.knowledge.graph.extraction import demo_extract, grounded
from app.services.llm_service import LLMService


async def test_l1_then_l2_warmup_expiry_and_singleflight():
    cache = Cache("", 30, l1_size=2, l1_ttl=3)
    redis = AsyncMock()
    cache.redis = redis
    redis.get.return_value = b'{"from":"redis"}'
    redis.pttl.return_value = 1000
    await cache.set("hot", {"from": "l1"})
    assert await cache.get("hot") == {"from": "l1"}
    redis.get.assert_not_awaited()
    assert await cache.get("cold") == {"from": "redis"}
    assert cache.local["cold"][0] <= time.monotonic() + 1
    await cache.get("cold")
    redis.get.assert_awaited_once()
    cache.redis = None
    cache.local.clear()
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return [1, 2]

    answers = await asyncio.gather(*(cache.get_or_load("one", loader) for _ in range(12)))
    assert calls == 1 and answers == [[1, 2]] * 12
    answers[0].append(3)
    assert await cache.get("one") == [1, 2]


def test_profiler_quantiles_window_and_alert_recovery(settings):
    settings.alert_min_samples = 2
    settings.alert_for_seconds = 10
    monitoring = Monitoring(settings)
    for i in range(1, 101):
        monitoring.record("llm", i / 100, i < 50)
    result = monitoring.snapshot()["llm"]
    assert result["p50_ms"] == 500 and result["p95_ms"] == 950 and result["p99_ms"] == 990
    monitoring.evaluate({"llm": result}, 0, {"neo4j": False}, now=0)
    assert monitoring.alerts["llm_error_rate"]["state"] == "pending"
    monitoring.evaluate({"llm": result}, 0, {"neo4j": False}, now=9)
    assert monitoring.alerts["llm_error_rate"]["state"] == "pending"
    monitoring.evaluate({"llm": result}, 0, {"neo4j": False}, now=10)
    assert monitoring.alerts["llm_error_rate"]["state"] == "firing"
    monitoring.evaluate({}, 0, {"neo4j": True}, now=11)
    assert monitoring.alerts["llm_error_rate"]["state"] == "normal"
    assert [h["to"] for h in monitoring.history if h["name"] == "neo4j_unavailable"] == [
        "pending",
        "firing",
        "normal",
    ]
    assert monitoring.snapshot(now=time.monotonic() + 301)["llm"]["count"] == 0


async def test_batch_is_bounded_ordered_and_cancellation_safe(settings):
    settings.mode = "live"
    settings.llm_max_concurrency = 2
    active = peak = 0

    async def provider(request):
        nonlocal active, peak
        body = json.loads(request.content)
        query = json.loads(body["messages"][-1]["content"])["query"]
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.03 if query % 2 else 0.01)
            return httpx.Response(200, json={"choices": [{"message": {"content": str(query)}}]})
        finally:
            active -= 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        service = LLMService(settings, client)
        assert await service.batch("rewrite", [{"query": i} for i in range(7)]) == [str(i) for i in range(7)]
        assert peak == 2
        task = asyncio.create_task(service.batch("rewrite", [{"query": i} for i in range(7)]))
        await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert active == 0
        assert await service.call("rewrite", {"query": 9}) == "9"


def test_domain_independent_extraction_and_grounding():
    text = "研究团队 --发现--> 蛋白质A\n蛋白质A --参与--> 免疫调节"
    extraction = demo_extract(text)
    entities, edges = grounded(extraction, text, 40)
    assert len(entities) == 3 and len(edges) == 2
    extraction["relations"].append(
        {"source": "蛋白质A", "target": "免疫调节", "predicate": "治疗", "evidence": "不存在的证据"}
    )
    assert len(grounded(extraction, text, 40)[1]) == 2


async def test_monitor_runs_without_polling_and_stops(settings):
    settings.alert_interval = 0.01
    settings.alert_for_seconds = 0.01
    monitoring = Monitoring(settings)
    await monitoring.start(AsyncMock(return_value={"neo4j": False}))
    await asyncio.sleep(0.08)
    assert monitoring.alerts["neo4j_unavailable"]["state"] == "firing"
    await monitoring.close()
    assert monitoring.task.done()


async def test_import_cpu_work_does_not_stall_event_loop(settings, monkeypatch):
    services = Container(settings)
    ticks = 0
    original = services.embedder._hash_encode

    def slow(texts):
        time.sleep(0.15)
        return original(texts)

    monkeypatch.setattr(services.embedder, "_hash_encode", slow)

    async def heartbeat():
        nonlocal ticks
        for _ in range(10):
            await asyncio.sleep(0.01)
            ticks += 1

    try:
        beat = asyncio.create_task(heartbeat())
        await services.embedder.embed(["跨行业知识" * 100])
        assert ticks >= 5
        await beat
    finally:
        await services.close()


async def test_langfuse_real_sdk_exports_trace_parent_usage_without_content(settings):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            received.append((self.path, raw))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings.langfuse_enabled = True
    settings.langfuse_public_key = "pk-lf-test-" + uuid.uuid4().hex
    from pydantic import SecretStr

    settings.langfuse_secret_key = SecretStr("test-secret")
    settings.langfuse_base_url = f"http://127.0.0.1:{server.server_port}"
    tracing = Tracing(settings, Monitoring(settings))
    try:
        await tracing.start()
        async with tracing.span("request", trace_id="a" * 32, input="PRIVATE-QUESTION"):
            async with tracing.span("llm", kind="generation", model="test-model") as generation:
                generation.update(output="PRIVATE-ANSWER", usage_details={"input": 12, "output": 8})
        await tracing.close()
        assert CURRENT_SPAN.get() is None
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

        spans = []
        for path, raw in received:
            request = ExportTraceServiceRequest.FromString(raw)
            spans.extend(
                s
                for resource in request.resource_spans
                for scope in resource.scope_spans
                for s in scope.spans
            )
            assert path.endswith("/v1/traces")
            assert b"PRIVATE-QUESTION" not in raw and b"PRIVATE-ANSWER" not in raw
        root = next(s for s in spans if s.name == "request")
        child = next(s for s in spans if s.name == "llm")
        assert root.trace_id == child.trace_id == bytes.fromhex("a" * 32)
        assert child.parent_span_id == root.span_id
        assert any("usage" in a.key for a in child.attributes)
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()


def test_observability_permissions_and_request_trace(client, admin):
    assert client.get("/api/v1/admin/observability").status_code == 401
    response = client.get("/api/v1/system", headers=admin)
    snapshot = client.get("/api/v1/admin/observability", headers=admin).json()
    assert any(span["trace_id"] == response.headers["x-request-id"] for span in snapshot["spans"])
    assert snapshot["pool"]["max_concurrency"] == 8
    assert client.get("/api/v1/graph/status", headers=admin).json()["enabled"] is False
    assert client.get("/api/v1/graph/network", headers=admin).status_code == 503


def test_metrics_machine_token_cannot_access_business_api(settings):
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from app.main import create_app

    settings.metrics_token = SecretStr("monitoring-only-token")
    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": "Bearer monitoring-only-token"}
        assert client.get("/api/v1/metrics", headers=headers).status_code == 200
        assert client.get("/api/v1/graph/status", headers=headers).status_code == 401
        assert client.get("/api/v1/admin/observability", headers=headers).status_code == 401


async def test_subclass_reasoning_requires_direction_and_predicate(settings):
    services = Container(settings)
    try:
        services.graph.context = AsyncMock(return_value=({}, "revision"))
        path = {
            "nodes": [
                {"id": "a", "name": "哺乳动物"},
                {"id": "b", "name": "脊椎动物"},
                {"id": "c", "name": "动物"},
            ],
            "links": [
                {"source": "a", "target": "b", "predicate": "子类于"},
                {"source": "b", "target": "c", "predicate": "子类于"},
            ],
        }
        services.graph.store.read = AsyncMock(return_value=[path])
        # This test isolates inference rules; encrypted graph serialization has separate integration tests.
        services.graph.decode_node = lambda value: value
        services.graph.decode_edge = lambda value: value
        result = await services.graph.paths("a", "c")
        assert result["inferences"][0]["target"] == "动物"
        path["links"][1]["predicate"] = "相关"
        assert (await services.graph.paths("a", "c"))["inferences"] == []
        path["links"][1].update(predicate="子类于", source="c", target="b")
        assert (await services.graph.paths("a", "c"))["inferences"] == []
    finally:
        await services.close()


async def test_retrieval_stack_singleton_and_local_index_snapshot(settings, sample, monkeypatch):
    services = Container(settings)
    try:
        await services.start()
        assert services.knowledge.embedder is services.embedder
        assert services.knowledge.store is services.store
        assert services.graph.llm is services.llm
        assert services.llm.client is services.http
        await services.knowledge.ingest("sample.md", sample)
        calls = 0
        original = services.repository.ready_chunks

        def read():
            nonlocal calls
            calls += 1
            return original()

        monkeypatch.setattr(services.repository, "ready_chunks", read)
        await services.knowledge.search("EUV", 4)
        await services.knowledge.search("芯片封装", 4)
        assert calls == 1
        await services.knowledge.ingest("new.md", "# 项目\n项目管理 --包含--> 需求分析".encode())
        await services.knowledge.search("项目管理", 4)
        assert calls == 2
    finally:
        await services.close()


async def test_live_entity_recognition_protocol_and_grounding(settings):
    settings.mode = "live"
    text = "林教授在星河大学研究免疫调节。"
    expected = {
        "entities": [{"name": "林教授", "kind": "人物"}, {"name": "星河大学", "kind": "组织"}],
        "relations": [{"source": "林教授", "target": "星河大学", "predicate": "任职于", "evidence": text}],
    }

    def provider(request):
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert json.loads(body["messages"][-1]["content"])["text"] == text
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(expected, ensure_ascii=False)}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        output = await LLMService(settings, client).call("extract_graph", {"text": text})
        entities, relations = grounded(output, text, 40)
        assert len(entities) == 2 and relations[0].predicate == "任职于"
