"""Per-answer timing through existing spans; no payloads, prompts or secrets."""

import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

CURRENT_TIMINGS = ContextVar("evaluation_answer_timings", default=None)


@contextmanager
def collect_timings():
    values = {}
    token = CURRENT_TIMINGS.set(values)
    try:
        yield values
    finally:
        CURRENT_TIMINGS.reset(token)


class TimedTracing:
    def __init__(self, tracing):
        self.tracing = tracing

    def __getattr__(self, name):
        return getattr(self.tracing, name)

    @asynccontextmanager
    async def span(self, name, **kwargs):
        started = time.perf_counter()
        success = False
        try:
            async with self.tracing.span(name, **kwargs) as observation:
                yield observation
                success = True
        finally:
            values = CURRENT_TIMINGS.get()
            if values is not None:
                task = (kwargs.get("metadata") or {}).get("task")
                key = f"{name}.{task}" if name == "llm" and task else name
                if name == "moderation":
                    key += "." + (kwargs.get("metadata") or {}).get("stage", "unknown")
                item = values.setdefault(key, {"calls": 0, "seconds": 0.0, "errors": 0})
                item["calls"] += 1
                item["seconds"] += time.perf_counter() - started
                item["errors"] += int(not success)


def install_timing(services):
    timed = TimedTracing(services.tracing)
    services.tracing = timed
    for service in (
        services.llm,
        services.embedder,
        services.knowledge,
        services.reranker,
        services.moderation,
        services.web,
    ):
        service.tracing = timed
