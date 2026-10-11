import asyncio

from app.common.context import knowledge_scope
from app.common.errors import AppError
from app.services.moderation_service import ModerationBlocked


class ChatService:
    def __init__(self, settings, repository, orchestrator, moderation):
        self.settings, self.repository, self.orchestrator = settings, repository, orchestrator
        self.moderation = moderation

    async def answer(self, conversation_id, user_id, question, mode, emit=None, base_ids=None):
        token = await asyncio.to_thread(self.repository.claim, conversation_id, user_id)
        scope_token = None
        try:
            scope = await asyncio.to_thread(
                self.repository.configure_scope, conversation_id, user_id, token, base_ids
            )
            history = await asyncio.to_thread(self.repository.history, conversation_id, user_id)
            history = [m for m in history if m["id"] > scope["scope_start_id"]]
            context = [{"role": m["role"], "content": m["content"][:3000]} for m in history[-10:]]
            scope_token = knowledge_scope.set(
                tuple(scope["base_ids"]) if scope["base_ids"] is not None else None
            )
            try:
                async with asyncio.timeout(self.settings.request_timeout):
                    if emit:
                        await emit(
                            {
                                "node": "moderation_input",
                                "label": "检查输入内容安全",
                                "sources": 0,
                                "rewrites": 0,
                                "generations": 0,
                            }
                        )
                    await self.moderation.guard(question, "input")
                    if context:
                        conversation = "\n".join(
                            ("历史用户：" if m["role"] == "user" else "历史助手：") + m["content"]
                            for m in context
                        )
                        await self.moderation.guard(conversation + "\n当前问题：" + question, "input")
                    result = await self.orchestrator.run(question, context, mode, emit)
                    if emit:
                        await emit(
                            {
                                "node": "moderation_output",
                                "label": "检查答案与引用内容安全",
                                "sources": len(result.get("sources", [])),
                                "rewrites": result["rewrites"],
                                "generations": result["generations"],
                            }
                        )
                    await self.moderation.guard(result["answer"], "output")
                    if result.get("sources"):
                        # The final transport envelope is checked too: metadata/citations cannot bypass review.
                        await self.moderation.guard_sources(result["sources"])
                    status = self.moderation.status()
                    result["safety"] = {
                        "decision": "allowed",
                        "backend": status["backend"],
                        "policy_version": status["policy_version"],
                    }
            except ModerationBlocked as blocked:
                return {
                    **self.moderation.blocked_result(blocked, mode, self.settings.mode, conversation_id),
                    "knowledge_scope": scope,
                }
            except TimeoutError as exc:
                raise AppError("generation_timeout", "本次问答超时，请稍后重试", 504) from exc
            result["knowledge_scope"] = scope
            await asyncio.to_thread(self.repository.finish, conversation_id, token, question, result)
            return {**result, "conversation_id": conversation_id}
        finally:
            if scope_token is not None:
                knowledge_scope.reset(scope_token)
            await asyncio.shield(asyncio.to_thread(self.repository.release, conversation_id, token))
