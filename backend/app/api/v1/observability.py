import asyncio

from fastapi import APIRouter, Depends

from app.dependencies import container, require_permission

router = APIRouter(
    prefix="/admin/observability",
    tags=["全链路可观测性"],
    dependencies=[Depends(require_permission("monitor.read"))],
)


@router.get("")
async def snapshot(services=Depends(container)):
    return {
        "scope": "current_process",
        "window_seconds": services.settings.profiler_window,
        "profiler": await asyncio.to_thread(services.monitoring.snapshot),
        "alerts": list(services.monitoring.alerts.values()),
        "history": list(services.monitoring.history),
        "cache": services.cache.stats(),
        "tracing": services.tracing.status(),
        "spans": list(services.tracing.recent)[-100:],
        "pool": {
            "connections": services.settings.llm_max_connections,
            "max_concurrency": services.settings.llm_max_concurrency,
            "batch_size": services.settings.llm_batch_size,
        },
        "dependencies": await services.dependency_health(),
        "moderation": services.moderation.status(),
        "reranker": services.reranker.status(),
        "memory": {
            "enabled": services.settings.memory_enabled,
            "worker_running": services.memory.worker is not None,
            "meter": services.settings.context_token_meter,
            "compaction_enabled": services.settings.context_compaction_enabled,
        },
    }
