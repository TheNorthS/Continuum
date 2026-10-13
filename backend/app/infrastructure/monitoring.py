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
            "rag_e_http_requests_total",# http请求累计次数
            "HTTP requests",            # 指标类型
            ["method", "route", "status"],# 三个标签维度
            registry=self.registry,
        )
        # 直方图记录耗时分布 
        self.latency = Histogram( 
            "rag_e_http_duration_seconds", "HTTP duration", ["route"], registry=self.registry
        )
        # 记录不同内部操作的成功和失败次数
        self.operations = Counter(
            "rag_e_operations_total", "Operations", ["operation", "status"], registry=self.registry
        )
        # 记录这些操作的耗时
        self.duration = Histogram(
            "rag_e_operation_duration_seconds", "Operation duration", ["operation"], registry=self.registry
        )
        # 累计LLM_token的使用量（模型服务商返回的）
        self.tokens = Counter(
            "rag_e_llm_tokens_total", "Provider-reported tokens", ["kind"], registry=self.registry
        )
        # 缓存命中、未命中等
        self.cache_events = Counter(
            "rag_e_cache_events_total",
            "Cache events",
            ["namespace", "tier", "result"],
            registry=self.registry,
        )
        # 当前进行中的 LLM 请求数
        self.inflight = Gauge("rag_e_llm_inflight", "Concurrent LLM requests", registry=self.registry)
        # asyncio 事件循环调度延迟
        self.loop_lag = Gauge(
            "rag_e_event_loop_lag_seconds", "Event loop scheduler lag", registry=self.registry
        )
        # 各规则当前告警状态
        self.alert_gauge = Gauge(
            "rag_e_alert_state", "0 normal, 1 pending, 2 firing", ["rule"], registry=self.registry
        )
        # 告警进入 firing 的累计次数
        self.firings = Counter(
            "rag_e_alert_firings_total", "Alert transitions to firing", ["rule"], registry=self.registry
        )
        # 保存操作采样 按条数限制的有界缓存
        self.samples = defaultdict(lambda: deque(maxlen=settings.profiler_samples))
        # 保护并发访问。
        self.lock = threading.Lock()
        # 存储每个告警规则的最新状态。
        self.alerts = {}
        # 保存最近 200 条告警状态变化。
        self.history = deque(maxlen=200)
        # 保存后台监控协程创建出的 Task
        self.task = None

    # 记录操作执行结果
    def record(self, operation, seconds, success=True):
        # 将原始操作存入队列 例：monitoring.record("retrieval", 0.35, True)
        with self.lock:
            # 记录单调时钟 持续时间 是否成功
            self.samples[operation].append((time.monotonic(), seconds, success))
        # 累计操作成功或失败次数
        self.operations.labels(operation, "ok" if success else "error").inc()
        # 记录耗时分布
        self.duration.labels(operation).observe(seconds)

    # 通过上下文管理器自动测量耗时
    # 执行 measure("llm")，获得上下文管理器。
    # 进入上下文，在 yield 前执行 start = time.perf_counter()，记录起始时间，并将 success 初始化为 False。
    # 执行 yield，将控制权交给 with 内部，开始运行 call_llm()。
    # 如果正常执行完毕，继续运行 success = True；如果内部抛出异常，则该赋值语句不会执行。
    # 无论正常还是异常，都执行 finally，计算耗时并调用 record()。异常随后继续向上传播。
    @contextmanager
    def measure(self, operation):
        start = time.perf_counter()
        success = False
        try:
            yield
            success = True
        finally:
            self.record(operation, time.perf_counter() - start, success)

    # 滑动窗口统计与 P50/P95/P99 计算，从有限的历史采样中筛选最近一段时间内的数据，计算各操作的延迟分布和错误数量。
    def snapshot(self, now=None):
        now = time.monotonic() if now is None else now
        # 复制采样数据
        with self.lock:
            copied = {name: list(values) for name, values in self.samples.items()}
        output = {}
        for name, samples in copied.items():
            # 计算窗口内的数据
            active = [s for s in samples if now - s[0] <= self.settings.profiler_window]
            ordered = sorted(s[1] for s in active)
            count = len(ordered)

            # 最近秩百分位算法
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
    # 管理告警状态和持续时间
    def transition(self, name, condition, value, threshold, now):
        alert = self.alerts.setdefault(name, {"name": name, "state": "normal", "since": None})
        previous = alert["state"]
        #  # 更新当前最新观测值及告警阈值。
        #
        # 例如：
        # value = 0.15
        # threshold = 0.1
        #
        # 表示当前错误率为 15%，阈值为 10%。
        #
        # update 不会删除原字典中的其他字段。
        alert.update(value=value, threshold=threshold)
        # 当前告警条件不成立
        if not condition:
            # 无论之前是 normal、pending 还是 firing
            alert.update(state="normal", since=None)
        else:
            #  先进入 pending 状态，而不是直接 firing。
            # since 记录首次检测到异常的单调时钟时间
            if previous == "normal":
                alert.update(state="pending", since=now)
            # 检查从首次检测到异常至今，已经过去多少秒。
            # alert_for_seconds 是告警持续时间要求。
            # 例如：
            # now = 1030
            # since = 1000
            # alert_for_seconds = 30
            # 1030 - 1000 >= 30 -> True
            if now - alert["since"] >= self.settings.alert_for_seconds:
                alert["state"] = "firing"
        self.alert_gauge.labels(name).set({"normal": 0, "pending": 1, "firing": 2}[alert["state"]])
        if alert["state"] != previous:
            self.history.append(
                {"name": name, "from": previous, "to": alert["state"], "at": time.time(), "value": value}
            )
            if alert["state"] == "firing":
                self.firings.labels(name).inc()
    # 比较指标与告警阈值
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
