import asyncio
import logging
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from contextvars import ContextVar

from app.common.redaction import redact

CURRENT_SPAN = ContextVar("rag_e_span", default=None)


class Observation:
    def __init__(self, tracing, remote=None):
        self.tracing, self.remote = tracing, remote

    def update(self, **values):
        if self.remote:
            try:
                if not self.tracing.settings.trace_capture_content:
                    values.pop("input", None)
                    values.pop("output", None)
                self.remote.update(**redact(values))
            except Exception:
                self.tracing.export_errors += 1


class Tracing:
    def __init__(self, settings, monitoring):
        self.settings, self.monitoring = settings, monitoring
        self.client = None
        self.recent = deque(maxlen=1000)
        self.export_errors = 0

    async def start(self):
        if self.settings.langfuse_enabled:
            from langfuse import Langfuse

            self.client = await asyncio.to_thread(
                Langfuse,
                public_key=self.settings.langfuse_public_key,
                secret_key=self.settings.langfuse_secret_key.get_secret_value(),
                base_url=self.settings.langfuse_base_url,
                environment=self.settings.environment,
                timeout=5,
                flush_at=32,
                flush_interval=5,
            )

    @asynccontextmanager
    async def span(self, name, *, trace_id=None, kind="span", model=None, metadata=None, input=None):
        parent = CURRENT_SPAN.get()
        tid = trace_id or (parent[0] if parent else uuid.uuid4().hex)
        remote = None
        if self.client:
            context = {"trace_id": tid}
            if parent and parent[0] == tid:
                context["parent_span_id"] = parent[1]
            try:
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
        span_id = remote.id if remote else uuid.uuid4().hex[:16]
        token = CURRENT_SPAN.set((tid, span_id))
        observation = Observation(self, remote)
        started = time.perf_counter()
        success = False
        try:
            yield observation
            success = True
        except BaseException as exc:
            observation.update(level="ERROR", status_message=type(exc).__name__)
            raise
        finally:
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
