import asyncio

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.api.schemas import SearchRequest
from app.common.context import knowledge_scope
from app.dependencies import container, require_permission

router = APIRouter(
    prefix="/knowledge", tags=["知识库"], dependencies=[Depends(require_permission("knowledge.read"))]
)


@router.get("/documents")
async def documents(services=Depends(container)):
    return await asyncio.to_thread(services.repository.documents)


@router.post("/documents", status_code=201, dependencies=[Depends(require_permission("knowledge.write"))])
async def upload(
    file: UploadFile = File(...), base_id: str = Form(default="legacy"), services=Depends(container)
):
    try:
        content = await file.read(services.settings.max_upload_bytes + 1)
        result = await services.knowledge.ingest(file.filename, content, base_id)
        await asyncio.to_thread(
            services.security.audit,
            "document.upload",
            result["id"],
            details={"base_id": base_id, "chunks": result["chunk_count"]},
        )
        return result
    finally:
        await file.close()


@router.delete(
    "/documents/{document_id}", status_code=204, dependencies=[Depends(require_permission("knowledge.write"))]
)
async def delete_document(document_id: str, services=Depends(container)):
    await services.knowledge.delete(document_id)


@router.post("/search")
async def search(payload: SearchRequest, services=Depends(container)):
    ids = await asyncio.to_thread(services.repository.validate_scope, payload.base_ids)
    token = knowledge_scope.set(tuple(ids) if ids is not None else None)
    try:
        return {"results": await services.knowledge.search(payload.query, payload.top_k)}
    finally:
        knowledge_scope.reset(token)
