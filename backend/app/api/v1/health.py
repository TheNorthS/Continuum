import asyncio
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.dependencies import bearer, container, current_user, require_permission

router = APIRouter(tags=["运行状态"])


@router.get("/health/live")
async def live():
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(services=Depends(container)):
    checks = {}
    for name, check in [("database", services.db.healthy), ("vector_store", services.store.healthy)]:
        try:
            await asyncio.wait_for(asyncio.to_thread(check), timeout=5)
            checks[name] = "ok"
        except Exception:
            checks[name] = "unavailable"
    good = all(v == "ok" for v in checks.values())
    if services.graph.enabled:
        try:
            await asyncio.wait_for(services.graph.store.healthy(), timeout=5)
            checks["neo4j"] = "ok"
        except Exception:
            checks["neo4j"] = "unavailable"
            good = False
    return JSONResponse(
        {"status": "ok" if good else "degraded", "checks": checks}, status_code=200 if good else 503
    )


@router.get("/system", dependencies=[Depends(current_user)])
async def system(services=Depends(container)):
    from app.pipeline_profile import pipeline_fingerprint

    settings = services.settings
    return {
        "version": "1.0.0",
        "mode": settings.mode,
        "vector_backend": settings.vector_backend,
        "embedding_provider": settings.embedding_provider,
        "web_search_enabled": settings.web_search_enabled,
        "web_search": services.web.status(),
        "vision": {"configured": services.vision.configured, "model": settings.vision_model or None},
        "graph_enabled": settings.graph_enabled,
        "knowledge_domain": settings.knowledge_domain,
        "langfuse_enabled": settings.langfuse_enabled,
        "max_upload_bytes": settings.max_upload_bytes,
        "moderation": services.moderation.status(),
        "reranker": services.reranker.status(),
        "pipeline_fingerprint": pipeline_fingerprint(settings),
        "pipeline_profile_enabled": settings.pipeline_profile is not None,
    }


@router.get("/admin/stats", dependencies=[Depends(require_permission("monitor.read"))])
async def stats(services=Depends(container)):
    return await asyncio.to_thread(services.repository.stats)


@router.get("/agents/workflow", dependencies=[Depends(current_user)])
async def workflow(services=Depends(container)):
    return {
        "mermaid": services.orchestrator.graph.get_graph().draw_mermaid(),
        "max_rewrites": services.settings.max_rewrites,
        "max_generations": services.settings.max_generations,
    }


async def metrics_auth(request: Request, credentials=Depends(bearer), services=Depends(container)):
    configured = services.settings.metrics_token.get_secret_value()
    if configured and credentials and secrets.compare_digest(configured, credentials.credentials):
        return
    await current_user(request, credentials, services)
    services.security.require("monitor.read")


@router.get("/metrics", dependencies=[Depends(metrics_auth)])
async def metrics(services=Depends(container)):
    return Response(generate_latest(services.registry), headers={"Content-Type": CONTENT_TYPE_LATEST})
