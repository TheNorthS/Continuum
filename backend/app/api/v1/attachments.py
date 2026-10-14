import asyncio
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.dependencies import container, require_permission

router = APIRouter(prefix="/attachments", tags=["个人空间"])
owner = require_permission("chat.use")


class Rename(BaseModel):
    filename: str = Field(min_length=1, max_length=180)


@router.get("")
async def listing(user=Depends(owner), services=Depends(container)):
    return await asyncio.to_thread(services.attachments.list, user["id"])


@router.post("", status_code=201)
async def upload(file: UploadFile = File(...), user=Depends(owner), services=Depends(container)):
    try:
        data = await file.read(services.settings.max_upload_bytes + 1)
        result = await services.attachments.upload(user["id"], file.filename, data)
        await asyncio.to_thread(
            services.security.audit, "attachment.upload", result["id"], details={"bytes": result["size"]}
        )
        return result
    finally:
        await file.close()


@router.get("/{attachment_id}")
async def detail(attachment_id: str, user=Depends(owner), services=Depends(container)):
    return await asyncio.to_thread(services.attachments.detail, user["id"], attachment_id)


@router.get("/{attachment_id}/content")
async def content(attachment_id: str, user=Depends(owner), services=Depends(container)):
    item, data = await asyncio.to_thread(services.attachments.download, user["id"], attachment_id)
    return Response(
        data,
        media_type=item["media_type"],
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(item["filename"], safe=""),
            "Cache-Control": "no-store",
            "Content-Security-Policy": "sandbox",
        },
    )


@router.patch("/{attachment_id}")
async def rename(attachment_id: str, payload: Rename, user=Depends(owner), services=Depends(container)):
    result = await asyncio.to_thread(services.attachments.rename, user["id"], attachment_id, payload.filename)
    await asyncio.to_thread(services.security.audit, "attachment.rename", attachment_id)
    return result


@router.delete("/{attachment_id}", status_code=204)
async def delete(attachment_id: str, user=Depends(owner), services=Depends(container)):
    await asyncio.to_thread(services.attachments.delete, user["id"], attachment_id)
    await asyncio.to_thread(services.security.audit, "attachment.delete", attachment_id)
