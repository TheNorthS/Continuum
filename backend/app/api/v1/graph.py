import asyncio

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.dependencies import container, require_permission

router = APIRouter(
    prefix="/graph", tags=["知识图谱"], dependencies=[Depends(require_permission("graph.read"))]
)


class PathQuery(BaseModel):
    source: str = Field(pattern=r"^[a-f0-9]{64}$")
    target: str = Field(pattern=r"^[a-f0-9]{64}$")
    hops: int = Field(default=3, ge=1, le=3)


@router.get("/status")
async def status(services=Depends(container)):
    return {
        "enabled": services.graph.enabled,
        "mode": services.settings.mode,
        "documents": await asyncio.to_thread(services.repository.documents),
    }


@router.get("/network")
async def network(
    query: str = Query(default="", max_length=200),
    limit: int = Query(default=100, ge=1, le=250),
    services=Depends(container),
):
    return await services.graph.network(query, limit)


@router.post("/paths")
async def paths(payload: PathQuery, services=Depends(container)):
    return await services.graph.paths(payload.source, payload.target, payload.hops)


@router.post("/documents/{document_id}/rebuild", dependencies=[Depends(require_permission("graph.write"))])
async def rebuild(document_id: str, services=Depends(container)):
    return await services.graph.index(document_id)
