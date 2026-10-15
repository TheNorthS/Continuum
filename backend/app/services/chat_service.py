import asyncio  # 提供线程卸载、整轮超时和取消时的释放保护。

from app.common.context import knowledge_scope  # 当前异步请求使用的知识库范围。
from app.common.errors import AppError  # 带业务错误码、公开提示和 HTTP 状态的异常。
from app.memory.context import ACTIVE_CONTEXT
from app.services.moderation_service import ModerationBlocked  # 内容被拦截时使用的专门异常。


class ChatService:  # 一轮问答的业务入口，负责锁、上下文、安全门控和最终持久化。
    def __init__(self, settings, repository, orchestrator, moderation):
        # 注入配置、数据访问层和图编排器，避免业务入口重复构造服务。
        self.settings, self.repository, self.orchestrator = settings, repository, orchestrator
        # 保存安全审查服务；attachments 由 Container 另行注入，复用同一附件空间实例。
        self.moderation = moderation

    async def answer(
        self,
        conversation_id,  # 当前会话 ID，数据层会检查是否属于 user_id。
        user_id,  # 已认证用户 ID，不能直接采用前端自报的身份。
        question,  # 用户本轮问题，接口层已限制长度。
        mode,  # adaptive 或 agentic，交给编排器选择路由方式。
        emit=None,  # 可选步骤回调；SSE 接口传入，普通 JSON 接口可以省略。
        base_ids=None,  # 指定本轮知识库；None 通常沿用会话保存的范围。
        use_knowledge=None,  # None 沿用会话设置，False 明确关闭知识库。
        attachment_ids=None,  # None 沿用会话已保存附件，[] 清空，列表替换选择。
    ):
        # 先验证归属并获取会话租约；同一会话同时生成时返回冲突，数据库操作在线程中执行。
        token = await asyncio.to_thread(self.repository.claim, conversation_id, user_id)
        # 记录 ContextVar 的恢复令牌；设置范围之前出错时仍能安全进入 finally。
        scope_token = None
        context_token = None
        context_state = None
        try:
            # 持有租约时核验并保存知识库范围；变更范围会刷新历史上下文起点。
            scope = await asyncio.to_thread(
                self.repository.configure_scope, conversation_id, user_id, token, base_ids, use_knowledge
            )
            # 持有同一租约保存/恢复附件选择，刷新后无需客户端再次传入全部 ID。
            selection = await asyncio.to_thread(
                self.repository.configure_attachments, conversation_id, user_id, token, attachment_ids
            )
            attachment_ids = selection["attachment_ids"]
            # 附件变化也会重置上下文起点，必须在读取历史之前使用最新边界。
            scope["scope_start_id"] = selection["scope_start_id"]
            # 设置请求局部范围：空元组表示关闭知识库，具体元组限定库，None 表示兼容的未固定范围。
            scope_token = knowledge_scope.set(
                ()
                if not scope["use_knowledge"]
                else tuple(scope["base_ids"])
                if scope["base_ids"] is not None
                else None
            )
            try:
                # 输入审查、附件识别、图执行和最终审查共享整轮预算；前面的锁/历史读取不在此计时内。
                async with asyncio.timeout(self.settings.request_timeout):
                    if emit:
                        # 先报告输入检查步骤，不发送未经审查的问题或候选答案正文。
                        await emit(
                            {
                                "node": "moderation_input",  # 前端识别步骤用的固定标识。
                                "label": "检查输入内容安全",  # 面向用户的步骤名称。
                                "sources": 0,  # 此时还没有读取本轮来源。
                                "rewrites": 0,  # 尚未开始问题改写。
                                "generations": 0,  # 尚未生成候选答案。
                            }
                        )
                    await self.moderation.guard(question, "input")  # 高风险问题在路由和检索前被拦截。
                    # 以可恢复事件和检查点构造历史，不再固定丢弃第 11 条之前的消息。
                    context_state = await self.context.open(user_id, conversation_id, token, question)
                    context_token = ACTIVE_CONTEXT.set(context_state)
                    context = self.context.history(context_state)
                    if context:
                        # 加上角色标签，便于审查器判断跨消息、分步提出的风险意图。
                        conversation = "\n".join(
                            ("历史用户：" if m["role"] == "user" else "历史助手：") + m["content"]
                            for m in context
                        )
                        # 审查实际要使用的历史与当前问题组合，避免只逐条看时遗漏上下文风险。
                        # 按重叠窗口检查组合上下文，保留角色和当前问题，避免逐条审查遗漏跨消息意图。
                        review = conversation + "\n当前问题：" + question
                        width = self.settings.moderation_max_chars
                        for start in range(0, len(review), width - 1000):
                            await self.moderation.guard(review[start : start + width], "input")
                    # 前者是给模型的证据，后者是不含二进制 payload 的附件摘要。
                    attachment_sources, selected_attachments = [], []
                    if attachment_ids:
                        if emit:
                            # 附件解析/识别可能耗时，单独展示进度；文件上传保存已在此前完成。
                            await emit(
                                {
                                    "node": "attachments",
                                    "label": "读取附件与识别图片",
                                    "sources": 0,
                                    "rewrites": 0,
                                    "generations": 0,
                                }
                            )
                        # 核验本人归属和总量，读取文本，按需调用独立视觉模型，并执行来源审查。
                        attachment_sources, selected_attachments = await self.attachments.sources(
                            user_id, attachment_ids, question
                        )
                    options = {}  # 普通知识库问答保留原编排调用形式。
                    # 有附件或关闭知识库时，明确传递新分支参数。
                    if attachment_sources or not scope["use_knowledge"]:
                        options = {
                            "attachment_sources": attachment_sources,  # 附件直接作为来源，不做向量化。
                            "use_knowledge": scope["use_knowledge"],  # 禁止编排器擅自打开知识库。
                        }
                    # 执行有界工作流，得到通过图内检查的最终答案或固定拒答。
                    result = await self.orchestrator.run(question, context, mode, emit, **options)
                    if selected_attachments:
                        # 保存本轮所选附件摘要，供界面展示及提交前删除/归属复核。
                        result["attachments"] = selected_attachments
                    if emit:
                        # 即将进入最终输出门控；此时仍只推送步骤信息。
                        await emit(
                            {
                                "node": "moderation_output",
                                "label": "检查答案与引用内容安全",
                                "sources": len(result.get("sources", [])),
                                "rewrites": result["rewrites"],
                                "generations": result["generations"],
                            }
                        )
                    # 即便图内已检查候选，真正准备返回与保存的答案仍需再次检查。
                    await self.moderation.guard(result["answer"], "output")
                    if result.get("sources"):
                        # 检查最终引用的正文和元数据，防止文件名、标题等绕过正文审查。
                        await self.moderation.guard_sources(result["sources"])
                    status = self.moderation.status()  # 读取有效审查后端和策略版本，不包含正文。
                    result["safety"] = {
                        "decision": "allowed",  # 表示本轮通过安全门控，不代表答案百分百正确。
                        "backend": status["backend"],  # local 或 llm 等实际审查后端。
                        "policy_version": status["policy_version"],  # 便于排查不同版本策略的行为。
                    }
            except ModerationBlocked as blocked:
                # 内容拦截返回固定替代回复；提前 return 会跳过 finish，不保存本轮问题/候选答案。
                return {
                    **self.moderation.blocked_result(blocked, mode, self.settings.mode, conversation_id),
                    "knowledge_scope": scope,
                }
            except TimeoutError as exc:
                # 将总预算超时转换为可公开的 504 错误，不暴露内部异常细节。
                raise AppError("generation_timeout", "本次问答超时，请稍后重试", 504) from exc
            # 在最终结果中说明实际使用的知识库范围和开关状态。
            result["knowledge_scope"] = scope
            result["context"] = self.context.report(context_state)
            # 提交前再次核验租约、来源权限和附件存在性，通过后原子写入用户与助手两条消息。
            await asyncio.to_thread(self.repository.finish, conversation_id, token, question, result)
            # 返回包含会话 ID 的结果，SSE 层收到后才能发送 result 事件。
            return {**result, "conversation_id": conversation_id}
        finally:
            if context_token is not None:
                ACTIVE_CONTEXT.reset(context_token)
            # 无论成功、拦截、异常还是取消，都恢复调用前的请求范围。
            if scope_token is not None:
                knowledge_scope.reset(scope_token)
            # 保护释放任务不被外层取消一并取消；释放只匹配本次 token，不解锁其他请求的租约。
            await asyncio.shield(asyncio.to_thread(self.repository.release, conversation_id, token))
