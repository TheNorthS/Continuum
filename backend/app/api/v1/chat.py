import asyncio
import contextlib
import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.api.schemas import AttachmentSelection, ChatRequest, ConversationCreate, ScopeRequest
from app.common.errors import AppError
from app.dependencies import container, current_user, require_permission

router = APIRouter(
    prefix="/conversations", tags=["会话"], dependencies=[Depends(require_permission("chat.use"))]
)


@router.get("")
async def conversations(user=Depends(current_user), services=Depends(container)):
    return await asyncio.to_thread(services.repository.conversations, user["id"])


@router.post("", status_code=201)
async def create(payload: ConversationCreate, user=Depends(current_user), services=Depends(container)):
    return await asyncio.to_thread(
        services.repository.create_conversation,
        user["id"],
        payload.title,
        payload.base_ids,
        payload.use_knowledge,
        payload.attachment_ids,
    )


@router.get("/{conversation_id}")
async def conversation(conversation_id: str, user=Depends(current_user), services=Depends(container)):
    return await asyncio.to_thread(services.repository.conversation, conversation_id, user["id"])


@router.patch("/{conversation_id}/scope")
async def scope(
    conversation_id: str, payload: ScopeRequest, user=Depends(current_user), services=Depends(container)
):
    return await asyncio.to_thread(
        services.repository.change_scope, conversation_id, user["id"], payload.base_ids, payload.use_knowledge
    )


@router.get("/{conversation_id}/messages")
async def messages(conversation_id: str, user=Depends(current_user), services=Depends(container)):
    return await asyncio.to_thread(services.repository.history, conversation_id, user["id"])


@router.patch("/{conversation_id}/attachments")
async def attachments(
    conversation_id: str,
    payload: AttachmentSelection,
    user=Depends(current_user),
    services=Depends(container),
):
    # 独立保存选择，不必先发出一条问题；与生成共用会话租约，禁止并发覆盖。
    return await asyncio.to_thread(
        services.repository.change_attachments, conversation_id, user["id"], payload.attachment_ids
    )


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str, user=Depends(current_user), services=Depends(container)):
    await asyncio.to_thread(services.repository.delete_conversation, conversation_id, user["id"])


@router.post("/{conversation_id}/messages")
async def answer(
    conversation_id: str, payload: ChatRequest, user=Depends(current_user), services=Depends(container)
):
    return await services.chat.answer(
        conversation_id,
        user["id"],
        payload.question,
        payload.mode,
        base_ids=payload.base_ids,
        use_knowledge=payload.use_knowledge,
        attachment_ids=payload.attachment_ids,
    )


@router.post("/{conversation_id}/stream")
async def stream(
    conversation_id: str, payload: ChatRequest, user=Depends(current_user), services=Depends(container)
):
    await asyncio.to_thread(
        services.repository.check_conversation_scope,
        conversation_id,
        user["id"],
        payload.base_ids,
        payload.use_knowledge,
    )

    async def events():
        queue = asyncio.Queue(maxsize=32)

        async def emit(step):
            await queue.put(("step", step))

        async def run():
            try:
                result = await services.chat.answer(
                    conversation_id,
                    user["id"],
                    payload.question,
                    payload.mode,
                    emit,
                    base_ids=payload.base_ids,
                    use_knowledge=payload.use_knowledge,
                    attachment_ids=payload.attachment_ids,
                )
                await queue.put(("result", result))
            except AppError as exc:
                await queue.put(("error", {"code": exc.code, "message": exc.message}))
            except Exception:
                await queue.put(("error", {"code": "internal_error", "message": "问答失败，请重试"}))

        task = asyncio.create_task(run())
        try:
            while True:
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=10)
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                if event in {"result", "error"}:
                    break
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
