import asyncio
from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.common.errors import AppError
from app.dependencies import container, require_permission
from app.infrastructure.conversation_lease import ConversationLease
from app.memory.policy import closed_segments
from app.models.entities import CompactionCheckpoint, MemoryRecord
from app.services.moderation_service import ModerationBlocked

router = APIRouter(tags=["记忆与上下文"])
owner = require_permission("chat.use")


class MemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    category: Literal["fact", "preference", "decision", "episode"] = "fact"
    subject: str = Field(min_length=1, max_length=120)
    predicate: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=1200)
    base_id: str | None = Field(default=None, min_length=1, max_length=32)
    expires_at: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class MemoryCorrection(MemoryInput):
    expected_version: int = Field(ge=1)


class MemoryPreferences(BaseModel):
    enabled: bool | None = None
    auto_extract: bool | None = None


class PinInput(BaseModel):
    event_ids: list[str] = Field(max_length=20)


class RestoreInput(BaseModel):
    checkpoint_id: str | None = Field(default=None, min_length=32, max_length=32)


@router.get("/memory")
async def memories(user=Depends(owner), services=Depends(container)):
    return {
        "items": await asyncio.to_thread(services.memory.listing, user["id"]),
        "preferences": await asyncio.to_thread(services.memory_store.preferences, user["id"]),
        "jobs": await asyncio.to_thread(services.memory.jobs, user["id"]),
    }


@router.patch("/memory/preferences")
async def preferences(payload: MemoryPreferences, user=Depends(owner), services=Depends(container)):
    return await asyncio.to_thread(
        services.memory_store.preferences, user["id"], payload.model_dump(exclude_none=True)
    )


@router.post("/memory", status_code=201)
async def remember(payload: MemoryInput, user=Depends(owner), services=Depends(container)):
    try:
        return await services.memory.manual(user["id"], payload.model_dump())
    except ModerationBlocked as exc:
        raise AppError("memory_blocked", "记忆内容未通过安全审查", 422) from exc


@router.post("/memory/reindex")
async def reindex(user=Depends(owner), services=Depends(container)):
    return await services.memory.reindex(user["id"])


@router.patch("/memory/{memory_id}")
async def correct(
    memory_id: str, payload: MemoryCorrection, user=Depends(owner), services=Depends(container)
):
    try:
        return await services.memory.manual(
            user["id"],
            payload.model_dump(exclude={"expected_version"}),
            memory_id=memory_id,
            expected_version=payload.expected_version,
        )
    except ModerationBlocked as exc:
        raise AppError("memory_blocked", "记忆内容未通过安全审查", 422) from exc


@router.delete("/memory/{memory_id}", status_code=204)
async def forget(memory_id: str, user=Depends(owner), services=Depends(container)):
    await asyncio.to_thread(services.memory_store.forget, user["id"], memory_id)


@router.delete("/memory", status_code=204)
async def forget_all(user=Depends(owner), services=Depends(container)):
    await asyncio.to_thread(services.memory_store.forget, user["id"])


@router.get("/memory/export/markdown")
async def export(user=Depends(owner), services=Depends(container)):
    value = await asyncio.to_thread(services.memory.export, user["id"])
    return Response(
        value,
        media_type="text/markdown",
        headers={"Content-Disposition": 'attachment; filename="MEMORY.md"', "Cache-Control": "no-store"},
    )


@router.get("/memory/{memory_id}/evidence")
async def evidence(memory_id: str, user=Depends(owner), services=Depends(container)):
    def read():
        with services.db.session() as session:
            row = session.get(MemoryRecord, memory_id)
            if not row or row.owner_id != user["id"] or row.status == "deleted":
                raise AppError("not_found", "记忆不存在", 404)
            ids = row.evidence_ids
        return services.memory_store.read_events(user["id"], ids)

    return {"events": await asyncio.to_thread(read)}


@router.get("/memory/artifacts/{artifact_id}")
async def artifact(artifact_id: str, user=Depends(owner), services=Depends(container)):
    return await asyncio.to_thread(services.memory_store.artifact, user["id"], artifact_id)


@router.get("/conversations/{conversation_id}/context")
async def context_status(conversation_id: str, user=Depends(owner), services=Depends(container)):
    snapshot = await asyncio.to_thread(services.memory_store.snapshot, conversation_id, user["id"])
    segments, _ = closed_segments(snapshot["events"])

    def checkpoints():
        with services.db.session() as session:
            rows = session.scalars(
                select(CompactionCheckpoint)
                .where(
                    CompactionCheckpoint.conversation_id == conversation_id,
                    CompactionCheckpoint.owner_id == user["id"],
                )
                .order_by(CompactionCheckpoint.created_at.desc())
                .limit(20)
            )
            return [
                {
                    "id": c.id,
                    "start_seq": c.start_seq,
                    "end_seq": c.end_seq,
                    "tokens_before": c.tokens_before,
                    "tokens_after": c.tokens_after,
                    "created_at": c.created_at,
                    "model": c.model,
                }
                for c in rows
            ]

    return {
        "revision": snapshot["revision"],
        "meter": services.settings.context_token_meter,
        "limits": services.token_meter.limits(services.settings.llm_model),
        "turns": len(segments),
        "checkpoint": snapshot["checkpoint"],
        "pins": snapshot["pins"],
        "events": [
            {
                "id": e["id"],
                "role": e["role"],
                "kind": e["kind"],
                "sequence": e["sequence"],
                "preview": e["content"]["text"][:120],
            }
            for e in snapshot["events"]
            if e["kind"] == "user_message"
        ],
        "checkpoints": await asyncio.to_thread(checkpoints),
    }


@router.put("/conversations/{conversation_id}/context/pins")
async def pins(conversation_id: str, payload: PinInput, user=Depends(owner), services=Depends(container)):
    await asyncio.to_thread(services.memory_store.set_pins, user["id"], conversation_id, payload.event_ids)
    return {"event_ids": payload.event_ids}


@router.post("/conversations/{conversation_id}/context/compact")
async def compact(conversation_id: str, user=Depends(owner), services=Depends(container)):
    async def operation(lease):
        async with asyncio.timeout(services.settings.request_timeout):
            current = await services.context.open(user["id"], conversation_id, lease.token, "继续当前会话")
            payload = {"question": "继续当前会话", "history": []}
            limits = services.token_meter.limits(services.settings.llm_model)
            committed = await services.context.compact(
                current, payload, "general", services.llm.body, limits, force=True
            )
            return {"committed": committed, **services.context.report(current)}

    try:
        return await ConversationLease(
            services.repository, services.settings, conversation_id, user["id"]
        ).run(operation)
    except ModerationBlocked as exc:
        raise AppError("context_blocked", "上下文摘要未通过安全审查，原始历史已保留", 422) from exc


@router.post("/conversations/{conversation_id}/memory/extract", status_code=202)
async def extract(conversation_id: str, user=Depends(owner), services=Depends(container)):
    return await asyncio.to_thread(services.memory.enqueue, user["id"], conversation_id)


@router.post("/conversations/{conversation_id}/context/restore")
async def restore(
    conversation_id: str, payload: RestoreInput, user=Depends(owner), services=Depends(container)
):
    return await asyncio.to_thread(
        services.memory_store.restore, user["id"], conversation_id, payload.checkpoint_id
    )
