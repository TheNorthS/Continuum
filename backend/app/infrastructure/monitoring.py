"""Bounded per-process profiler and continuously evaluated alert state machines."""

import asyncio
import contextlib
import math
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class Monitoring:
    def __init__(self, settings):
        self.settings = settings
        # Prometheus客户端的指标注册容器
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "rag_e_http_requests_total",
            "HTTP requests",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "rag_e_http_duration_seconds", "HTTP duration", ["route"], registry=self.registry
        )
        self.operations = Counter(
            "rag_e_operations_total", "Operations", ["operation", "status"], registry=self.registry
        )
        self.duration = Histogram(
            "rag_e_operation_duration_seconds", "Operation duration", ["operation"], registry=self.registry
        )
        self.tokens = Counter(
            "rag_e_llm_tokens_total", "Provider-reported tokens", ["kind"], registry=self.registry
        )
        self.cache_events = Counter(
            "rag_e_cache_events_total",
            "Cache events",
            ["namespace", "tier", "result"],
            registry=self.registry,
        )
        self.inflight = Gauge("rag_e_llm_inflight", "Concurrent LLM requests", registry=self.registry)
        self.loop_lag = Gauge(
            "rag_e_event_loop_lag_seconds", "Event loop scheduler lag", registry=self.registry
        )
        self.alert_gauge = Gauge(
            "rag_e_alert_state", "0 normal, 1 pending, 2 firing", ["rule"], registry=self.registry
        )
        self.firings = Counter(
            "rag_e_alert_firings_total", "Alert transitions to firing", ["rule"], registry=self.registry
        )
        self.samples = defaultdict(lambda: deque(maxlen=settings.profiler_samples))
        self.lock = threading.Lock()
        self.alerts = {}
        self.history = deque(maxlen=200)
        self.task = None

    def record(self, operation, seconds, success=True):
        with self.lock:
            self.samples[operation].append((time.monotonic(), seconds, success))
        self.operations.labels(operation, "ok" if success else "error").inc()
        self.duration.labels(operation).observe(seconds)

    @contextmanager
    def measure(self, operation):
        start = time.perf_counter()
        success = False
        try:
            yield
            success = True
        finally:
            self.record(operation, time.perf_counter() - start, success)

    def snapshot(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            copied = {name: list(values) for name, values in self.samples.items()}
        output = {}
        for name, samples in copied.items():
            active = [s for s in samples if now - s[0] <= self.settings.profiler_window]
            ordered = sorted(s[1] for s in active)
            count = len(ordered)

            def percentile(p, ordered=ordered, count=count):
                return ordered[max(0, math.ceil(p * count) - 1)] if count else 0

            output[name] = {
                "count": count,
                "errors": sum(not s[2] for s in active),
                "p50_ms": percentile(0.5) * 1000,
                "p95_ms": percentile(0.95) * 1000,
                "p99_ms": percentile(0.99) * 1000,
                "average_ms": sum(ordered) / count * 1000 if count else 0,
            }
        return output

    def transition(self, name, condition, value, threshold, now):
        alert = self.alerts.setdefault(name, {"name": name, "state": "normal", "since": None})
        previous = alert["state"]
        alert.update(value=value, threshold=threshold)
        if not condition:
            alert.update(state="normal", since=None)
        else:
            if previous == "normal":
                alert.update(state="pending", since=now)
            if now - alert["since"] >= self.settings.alert_for_seconds:
                alert["state"] = "firing"
        self.alert_gauge.labels(name).set({"normal": 0, "pending": 1, "firing": 2}[alert["state"]])
        if alert["state"] != previous:
            self.history.append(
                {"name": name, "from": previous, "to": alert["state"], "at": time.time(), "value": value}
            )
            if alert["state"] == "firing":
                self.firings.labels(name).inc()

    def evaluate(self, snapshot, lag, dependencies, now=None):
        now = time.monotonic() if now is None else now
        for operation in ("http", "llm", "retrieval", "rerank", "cypher", "moderation"):
            item = snapshot.get(operation, {})
            count = item.get("count", 0)
            enough = count >= self.settings.alert_min_samples
            rate = item.get("errors", 0) / count if count else 0
            p95 = item.get("p95_ms", 0) / 1000
            self.transition(
                operation + "_error_rate",
                enough and rate >= self.settings.alert_error_rate,
                rate,
                self.settings.alert_error_rate,
                now,
            )
            self.transition(
                operation + "_p95",
                enough and p95 >= self.settings.alert_p95_seconds,
                p95,
                self.settings.alert_p95_seconds,
                now,
            )
        self.transition(
            "event_loop_lag",
            lag >= self.settings.alert_loop_lag_seconds,
            lag,
            self.settings.alert_loop_lag_seconds,
            now,
        )
        for name, healthy in dependencies.items():
            self.transition(name + "_unavailable", not healthy, 0 if healthy else 1, 1, now)

    async def start(self, health):
        async def monitor():
            while True:
                expected = asyncio.get_running_loop().time() + self.settings.alert_interval
                await asyncio.sleep(self.settings.alert_interval)
                lag = max(0, asyncio.get_running_loop().time() - expected)
                self.loop_lag.set(lag)
                self.record("event_loop", lag)
                dependencies = await health()
                snapshot = await asyncio.to_thread(self.snapshot)
                self.evaluate(snapshot, lag, dependencies)

        self.task = asyncio.create_task(monitor(), name="rag-e-monitor")

    async def close(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
