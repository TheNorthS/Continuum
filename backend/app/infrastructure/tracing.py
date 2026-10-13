import asyncio
import logging
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from contextvars import ContextVar

from app.common.redaction import redact

# 让每个异步任务维护自己当前正在执行的 Span，同时允许嵌套调用继承父级上下文。
CURRENT_SPAN = ContextVar("rag_e_span", default=None)


class Observation:
    def __init__(self, tracing, remote=None):
        # 当前 Tracing 实例，用于访问配置和错误计数
        # Langfuse SDK 创建的 Observation 对象。如果未启用 Langfuse 或创建失败，则可能是 None
        self.tracing, self.remote = tracing, remote

    def update(self, **values):
        # 检查远程对象是否存在（langfuse）
        if self.remote:
            try:
                if not self.tracing.settings.trace_capture_content:
                    values.pop("input", None)
                    values.pop("output", None)
                # 进行脱敏并更新远程数据
                self.remote.update(**redact(values))
            except Exception:
                self.tracing.export_errors += 1


class Tracing:
    def __init__(self, settings, monitoring):
        self.settings, self.monitoring = settings, monitoring
        # Langfuse客户端
        self.client = None
        # 用双端队列保存最近的1000条Span
        self.recent = deque(maxlen=1000)
        self.export_errors = 0

    async def start(self):
        if self.settings.langfuse_enabled:
            from langfuse import Langfuse

            self.client = await asyncio.to_thread(
                Langfuse,
                public_key=self.settings.langfuse_public_key, # 公钥
                secret_key=self.settings.langfuse_secret_key.get_secret_value(), # 私钥
                base_url=self.settings.langfuse_base_url, # 远端地址
                environment=self.settings.environment, # 开发 or 正式
                timeout=5, # 超时时间
                flush_at=32, # 批处理缓冲达到配置阈值时触发发送
                flush_interval=5, # 周期性触发缓冲数据发送，通常以秒为单位
            )

    @asynccontextmanager
    async def span(self, name, *, trace_id=None, kind="span", model=None, metadata=None, input=None):
        # # CURRENT_SPAN 是 ContextVar 对象。
        #     #
        #     # 获取当前异步执行上下文中保存的 Span 信息。
        #     #
        #     # CURRENT_SPAN 中保存的数据格式：
        #     # (trace_id, span_id)
        #     #
        #     # 情况一：当前没有任何活动 Span
        #     # parent = None
        #     #
        #     # 情况二：当前已经进入某个父 Span
        #     # parent = ("T100", "SPAN_A")
        #     #
        #     # ContextVar 能够在 asyncio 并发任务中隔离上下文，
        #     # 避免不同请求互相覆盖当前 Span 信息。
        parent = CURRENT_SPAN.get()

        # # 确定当前 Span 所属的 Trace ID。
        #     #
        #     # 这里使用 Python 的 or 和条件表达式。
        #     #
        #     # 优先级：
        #     #
        #     # 1. 如果显式传入了非空 trace_id，就使用它
        #     #
        #     # 2. 如果没有传入 trace_id，但存在父 Span，
        #     #    就继承父 Span 的 Trace ID，即 parent[0]
        #     #
        #     # 3. 如果既没有传入 trace_id，也不存在父 Span，
        #     #    就使用 uuid.uuid4().hex 生成新的 Trace ID
        #     #
        #     # 例如：
        #     #
        #     # trace_id="T200" -> tid="T200"
        #     #
        #     # trace_id=None, parent=("T100", "A")
        #     #   -> tid="T100"
        #     #
        #     # trace_id=None, parent=None
        #     #   -> 生成新的 32 位十六进制 UUID 字符串
        tid = trace_id or (parent[0] if parent else uuid.uuid4().hex)
        remote = None
        if self.client:
            context = {"trace_id": tid}
            # tid 相同才建立父子关系
            if parent and parent[0] == tid:
                context["parent_span_id"] = parent[1]
            # 尝试创建远程 Observation。
            try:
                # 调用 Langfuse SDK，开始一个 Observation。
                # 这个调用会返回代表该 Observation 的对象。
                # 注意：不一定意味着已经发送到 Langfuse 服务器，
                # SDK 可能先将数据放入内部缓冲队列。
                remote = self.client.start_observation(
                    name=name,
                    as_type=kind,
                    trace_context=context,
                    model=model,
                    metadata=metadata,
                    input=redact(input) if self.settings.trace_capture_content else None,
                )
            except Exception:
                self.export_errors += 1
        # 创建 Span ID, 远程有用远程的，远程没有本地生成取前16位
        span_id = remote.id if remote else uuid.uuid4().hex[:16]
        # 将当前span保存到异步执行的上下文
        token = CURRENT_SPAN.set((tid, span_id))
        # 创建 Observation 包装对象
        observation = Observation(self, remote)
        started = time.perf_counter()
        success = False
        try:
            # 这是 asynccontextmanager 最重要的一行。
            # 执行到这里时，暂停当前异步生成器，
            # 并将 observation 对象交给 async with。
            # 例如业务代码：
            # async with tracing.span("embedding") as obs:
            #     result = await embedding()
            #     obs.update(output=result)
            # 这里的 obs 就是 observation。
            # yield 之后，真正开始执行 async with 内部的代码。
            # 如果业务代码正常退出 async with，
            # 上下文管理器恢复执行 yield 后面的代码。
            # 如果业务代码抛出异常，
            # 异常会通过上下文管理器机制传回 yield 位置。
            yield observation
            success = True
        except BaseException as exc:
            observation.update(level="ERROR", status_message=type(exc).__name__)
            raise
        finally:
            # 记录操作耗时
            duration = time.perf_counter() - started
            self.monitoring.record(name, duration, success)
            self.recent.append(
                {
                    "trace_id": tid,
                    "span_id": span_id,
                    "parent_id": parent[1] if parent else None,
                    "name": name,
                    "duration_ms": duration * 1000,
                    "status": "ok" if success else "error",
                    "at": time.time(),
                }
            )
            if remote:
                try:
                    remote.end()
                except Exception:
                    self.export_errors += 1
            CURRENT_SPAN.reset(token)

    async def close(self):
        if self.client:
            try:
                await asyncio.to_thread(self.client.flush)
                await asyncio.to_thread(self.client.shutdown)
            except Exception:
                logging.getLogger(__name__).warning("langfuse_shutdown_failed")

    def status(self):
        return {
            "enabled": self.settings.langfuse_enabled,
            "initialized": self.client is not None,
            "capture_content": self.settings.trace_capture_content,
            "instrumentation_errors": self.export_errors,
        }
