# SQL 是记忆的权威来源；所有历史、摘要和记忆投影都要按实时归属与证据重新校验。
"""SQL is authoritative; every projection is checked against live ownership and evidence."""

# 记录租约、失效时间、使用时间和遗忘边界。
import time

# 构建查询、聚合、条件更新和批量删除语句。
from sqlalchemy import delete, func, select, update

# 复用请求身份上下文，使知识库 ACL 检查使用指定用户。
from app.common.context import actor

# 统一返回权限、来源失效和并发冲突错误。
from app.common.errors import AppError

# 使用完整回合切分和稳定内容指纹。
from app.memory.policy import closed_segments, digest

# 导入本模块涉及的业务表和记忆持久化实体。
from app.models.entities import (
    # 附件实体用于检查私人文件归属与存在性。
    Attachment,
    # 压缩检查点记录摘要、覆盖范围和原文指纹。
    CompactionCheckpoint,
    # 会话实体保存资料范围、活动检查点、版本与租约。
    Conversation,
    # 文档实体用于核对来源是否仍处于 ready 状态。
    Document,
    # 知识库实体提供当前检索版本。
    KnowledgeBase,
    # 资料实体保存历史证据正文及其依赖。
    MemoryArtifact,
    # 候选表支持提炼与整合分阶段持久化。
    MemoryCandidate,
    # 事件表记录用户、助手与证据引用历史。
    MemoryEvent,
    # 任务表保存后台提炼阶段、状态与租约。
    MemoryJob,
    # 记忆记录表保存版本化事实及其证据。
    MemoryRecord,
    # 用户记忆状态表保存总开关、版本和遗忘时间边界。
    MemoryState,
    # 已持久化聊天消息是事件同步的输入。
    Message,
    # 用户实体用于实时账号权限复核及串行化写入。
    User,
    # 生成资料、工具配对和检查点的新标识。
    new_id,
)


# 将 ORM 事件转换成可哈希、可传递的普通字典。
def event_dict(row):
    # 仅暴露上下文处理需要的事件属性。
    return {
        # 真实事件 ID 用于摘要引用和证据回读。
        "id": row.id,
        # 会话内序号用于排序及确定检查点覆盖边界。
        "sequence": row.sequence,
        # 关联原始聊天消息，支持增量同步和范围过滤。
        "message_id": row.message_id,
        # 事件类型区分用户消息、助手消息、工具调用及结果。
        "kind": row.kind,
        # 角色用于模型历史投影。
        "role": row.role,
        # 工具调用 ID 用于验证调用与结果成对出现。
        "tool_call_id": row.tool_call_id,
        # 包含事件正文及来源依赖；加密字段由模型类型处理解密。
        "content": row.content,
        # 保留原始时间，供遗忘边界等逻辑使用。
        "created_at": row.created_at,
    }


# 封装记忆持久化、授权、来源校验和检查点事务。
class MemoryStore:
    # 接收数据库、安全服务与现有会话仓储。
    def __init__(self, settings, db, security, repository):
        # 复用应用级依赖，不创建独立业务数据库。
        self.settings, self.db, self.security, self.repository = settings, db, security, repository

    # 读取或初始化用户记忆设置，必要时先串行化该用户的写事务。
    def state(self, session, uid, *, lock=False):
        # 写入路径可显式要求加锁。
        if lock:
            # 执行用户行的等值更新来获得数据库写锁，不改变 token_version 数值。
            session.execute(update(User).where(User.id == uid).values(token_version=User.token_version))
        # 强制刷新账号数据，避免身份信息被会话缓存遮蔽。
        user = session.get(User, uid, populate_existing=True)
        # 被删除或停用的账号不能读取或维护记忆。
        if not user or not user.active:
            # 统一用不存在错误隐藏不可访问账号状态。
            raise AppError("not_found", "账号不存在或已停用", 404)
        # 记忆能力与问答权限绑定，实时检查角色权限。
        if "chat.use" not in self.security.permissions(session, user.role):
            # 缺少 chat.use 时拒绝访问。
            raise AppError("forbidden", "当前账号无记忆与问答权限", 403)
        # 从数据库刷新用户记忆开关与版本。
        state = session.get(MemoryState, uid, populate_existing=True)
        # 首次使用时可能尚无记忆状态行。
        if state is None:
            # First-use initialization also serializes across workers, not only explicit mutations.
            # 初始化也要先锁住用户行，避免多 worker 并发创建。
            session.execute(update(User).where(User.id == uid).values(token_version=User.token_version))
            # 获得写锁后再次读取，兼容另一个事务刚完成的初始化。
            state = session.get(MemoryState, uid, populate_existing=True)
        # 复查仍不存在才创建默认状态。
        if state is None:
            # 新建与用户 ID 一一对应的设置行。
            state = MemoryState(
                # 沿用全局开关，默认允许自动提炼，版本与遗忘边界从零开始。
                id=uid,
                # 新用户的记忆总开关继承全局配置。
                enabled=self.settings.memory_enabled,
                # 默认允许自动提炼，用户可以在管理界面关闭。
                auto_extract=True,
                # 个人状态版本从零开始递增。
                version=0,
                # 初始没有遗忘时间边界。
                forget_before=0,
            )
            # 将新状态加入当前事务。
            session.add(state)
            # 立即落到事务内，使后续查询能读到初始化结果。
            session.flush()
        # 返回当前用户的权威记忆状态。
        return state

    # 以显式用户身份查询可见知识库，复用既有 ACL 规则。
    def visible_bases(self, session, uid):
        # 临时绑定用户身份，并保存旧上下文令牌。
        token = actor.set({"id": uid})
        # 保证查询无论成功或异常都恢复之前的身份。
        try:
            # 返回可见知识库 ID 集合，方便做子集判断。
            return set(self.security.visible_bases(session))
        # 在退出时执行身份清理。
        finally:
            # 恢复调用前的 actor，避免后续请求串用身份。
            actor.reset(token)

    # 范围构造只依赖会话记录，不依赖存储实例。
    @staticmethod
    # 提取决定历史和记忆可用性的会话范围字段。
    def scope(row):
        # 返回可生成稳定指纹的范围字典。
        return {
            # 是否允许使用知识库内容。
            "use_knowledge": row.use_knowledge,
            # 选中的知识库 ID；None 与空列表有不同业务含义。
            "base_ids": row.base_ids,
            # 当前会话持久化选择的私人附件。
            "attachment_ids": row.attachment_ids,
            # 范围切换后允许纳入历史的消息边界。
            "start_id": row.scope_start_id,
        }

    # 从已提交回答详情中提取其依赖的知识库、文档、附件和记忆。
    def dependencies(self, session, details):
        # 读取回答时记录的知识范围，缺省使用空映射。
        scope = details.get("knowledge_scope") or {}
        # 收集来源中明确标注的文档 ID，并去重。
        documents = {
            # 从来源元数据提取文档身份。
            s["metadata"]["document_id"]
            # 遍历回答实际使用的来源。
            for s in details.get("sources", [])
            # 忽略不属于知识文档的来源，例如纯记忆条目。
            if s.get("metadata", {}).get("document_id")
        }
        # 先以回答记录的选库范围作为知识库依赖集合。
        bases = set(scope.get("base_ids") or [])
        # 查回来源文档，补充其真实所属知识库。
        for doc in session.scalars(select(Document).where(Document.id.in_(documents))):
            # 避免来源关联库未出现在显式范围字段中而被漏掉。
            bases.add(doc.base_id)
        # 为所有依赖知识库保存内容版本快照。
        revisions = {
            # 每个知识库 ID 对应当时的 retrieval_revision。
            b.id: b.retrieval_revision
            # 只读取当前依赖集合中的知识库记录。
            for b in session.scalars(select(KnowledgeBase).where(KnowledgeBase.id.in_(bases)))
        }
        # 返回后续每次回读都需要复核的依赖信息。
        return {
            # 库内容版本变化会使依赖它的历史投影失效。
            "bases": revisions,
            # 保存排序后的来源文档 ID，保证表示稳定。
            "documents": sorted(documents),
            # 记录回答使用的私人附件。
            "attachments": [a["id"] for a in details.get("attachments", [])],
            # 记录本轮实际使用过的记忆，形成可追溯的依赖链。
            "memories": details.get("context", {}).get("memory_ids", []),
        }

    # 递归校验证据依赖，并阻止记忆之间出现循环引用。
    def accessible(self, session, uid, deps, scope=None, visited=None):
        # 初始化本条依赖链已经访问过的记忆 ID 集合。
        visited = visited or set()
        # 获取需要检查内容版本的知识库映射。
        bases = deps.get("bases", {})
        # 合并版本依赖与只要求访问权的 base_access 依赖。
        required_bases = set(bases) | set(deps.get("base_access", []))
        # 任何依赖知识库不再可见，都不能复用该内容。
        if not required_bases.issubset(self.visible_bases(session, uid)):
            # 拒绝通过历史或记忆绕过知识库授权。
            return False
        # 逐个检查知识库的存在性和内容版本。
        for bid, revision in bases.items():
            # 刷新知识库记录，读取最新版本。
            row = session.get(KnowledgeBase, bid, populate_existing=True)
            # 知识库被删除或内容发生变化时旧依赖失效。
            if row is None or row.retrieval_revision != revision:
                # 返回不可用，由调用方丢弃或报错。
                return False
        # 提供会话范围时还要执行本轮的选库和附件限制。
        if scope is not None:
            # 涉及知识库内容时，检查以下范围约束。
            if required_bases and (
                # 关闭知识库的对话不能借记忆引用库内内容。
                not scope["use_knowledge"]
                # 显式限定知识库集合时，继续做子集检查。
                or scope["base_ids"] is not None
                # 所需知识库必须全部属于本轮选择集合。
                and not required_bases.issubset(scope["base_ids"])
            ):
                # 范围不满足则拒绝该依赖。
                return False
            # 历史使用的附件必须仍在当前会话选择列表中。
            if not set(deps.get("attachments", [])).issubset(scope["attachment_ids"]):
                # 已从本轮附件范围移除的资料不能继续注入。
                return False
        # 逐个检查依赖附件的实际存储状态。
        for aid in deps.get("attachments", []):
            # 刷新附件记录，避免使用缓存中的旧归属。
            row = session.get(Attachment, aid, populate_existing=True)
            # 删除的附件或其他用户的附件均不可访问。
            if row is None or row.owner_id != uid:
                # 拒绝无效附件来源。
                return False
        # 逐个检查原始知识文档。
        for did in deps.get("documents", []):
            # 从数据库刷新文档状态。
            row = session.get(Document, did, populate_existing=True)
            # 文档必须存在且索引处于 ready 状态。
            if row is None or row.status != "ready":
                # 删除、处理中或失败的文档不能作为有效证据。
                return False
        # 继续递归检查当前内容所依赖的其他记忆。
        for mid in deps.get("memories", []):
            # 读取最新个人记忆开关。
            state = session.get(MemoryState, uid, populate_existing=True)
            # 用户未初始化或关闭记忆时，不再复用记忆依赖。
            if state is None or not state.enabled:
                # 将当前依赖链判为不可访问。
                return False
            # 遇到已访问 ID 或过长依赖链时停止递归。
            if mid in visited or len(visited) > 32:
                # 拒绝循环或过深依赖，避免无限回溯。
                return False
            # 刷新被引用的记忆版本和状态。
            row = session.get(MemoryRecord, mid, populate_existing=True)
            # 把该 ID 加入访问集合，并递归检查记忆自身的证据。
            if not self.record_accessible(session, uid, row, scope, visited | {mid}):
                # 任何下游依赖失效都使当前内容失效。
                return False
        # 全部库、文档、附件和记忆依赖均通过时才允许使用。
        return True

    # 判断单条记忆是否能被当前用户在给定范围内实际使用。
    def record_accessible(self, session, uid, row, scope=None, visited=None):
        # 首先检查记忆存在性、归属、状态和有效期。
        if (
            # 数据库中已不存在的记录不可用。
            row is None
            # 仅允许当前用户使用自己的个人记忆。
            or row.owner_id != uid
            # 争议、被替代或删除状态都不会注入模型。
            or row.status != "active"
            # 设置了有效期的记录需要继续比较到期时间。
            or row.expires_at
            # 已经到期的记忆不可复用。
            and row.expires_at <= time.time()
        ):
            # 以上任一条件满足时返回不可用。
            return False
        # 关联知识库的记忆额外受该库 ACL 约束。
        if row.base_id:
            # 当前用户必须仍能访问关联知识库。
            if row.base_id not in self.visible_bases(session, uid):
                # 撤销库权限后，不允许通过记忆泄露原有内容。
                return False
            # 有会话范围时，关联库也必须符合本轮选择。
            if scope is not None and (
                # 知识库关闭时排除库范围记忆。
                not scope["use_knowledge"]
                # 显式选库时继续核对关联库。
                or scope["base_ids"] is not None
                # 未被本轮选中的知识库记忆不参与作答。
                and row.base_id not in scope["base_ids"]
            ):
                # 返回范围不匹配。
                return False
        # 最后检查所有原始证据，包括它们继续引用的其他资源。
        return self.evidence_accessible(session, uid, row.evidence_ids, scope, visited)

    # 核验一组原始事件是否仍属于当前用户且依赖有效。
    def evidence_accessible(self, session, uid, ids, scope=None, visited=None):
        # 没有任何证据的记忆不能通过此校验。
        if not ids:
            # 用 False 明确表示缺少来源支持。
            return False
        # 对每一个声明的证据 ID 分别检查。
        for eid in ids:
            # 查询对应的原始事件。
            event = session.get(MemoryEvent, eid)
            # 事件存在、归属与依赖条件必须同时成立。
            if (
                # 缺失事件意味着原始来源不可回读。
                event is None
                # 禁止引用其他用户的历史作为个人记忆证据。
                or event.owner_id != uid
                # 继续校验事件携带的知识库、文档、附件及记忆依赖。
                or not self.accessible(session, uid, event.content.get("deps", {}), scope, visited)
            ):
                # 发现一条无效证据就拒绝整组。
                return False
        # 全部事件通过才允许使用其派生记忆或摘要。
        return True

    # 把已经成功提交的聊天消息增量同步为事件；不在此保存未通过审查的候选。
    def sync_events(self, session, conversation):
        """Adopt approved message history; never persist rejected inputs or candidates."""
        # 计算当前会话已经投影到事件表的最大消息 ID。
        last_message_id = (
            # 执行标量聚合查询。
            session.scalar(
                # 只统计本会话，避免不同会话的消息水位混淆。
                select(func.max(MemoryEvent.message_id)).where(MemoryEvent.conversation_id == conversation.id)
            )
            # 没有历史事件时从零水位开始。
            or 0
        )
        # 计算该会话当前最大事件序号。
        sequence = (
            # 查询事件表的序号最大值。
            session.scalar(
                # 事件序号只在当前会话内递增。
                select(func.max(MemoryEvent.sequence)).where(MemoryEvent.conversation_id == conversation.id)
            )
            # 首次同步从零开始分配。
            or 0
        )
        # 读取尚未转换成事件的已提交消息。
        messages = session.scalars(
            # 以原始消息表为输入。
            select(Message)
            # 同时限定会话身份和上次同步后的消息 ID。
            .where(Message.conversation_id == conversation.id, Message.id > last_message_id)
            # 按消息顺序处理，保证工具证据和助手回复顺序稳定。
            .order_by(Message.id)
            # 取出完整待同步消息列表。
        ).all()
        # 遍历消息时保留位置，便于查看紧邻的助手回复。
        for index, message in enumerate(messages):
            # 默认使用当前消息已有的回答详情。
            details = message.details or {}
            # 用户消息可借用紧随其后的助手详情补齐本回合资料依赖。
            if (
                # 当前消息必须来自用户。
                message.role == "user"
                # 必须确实存在下一条消息。
                and index + 1 < len(messages)
                # 下一条必须是助手回复，不能错借其他角色的信息。
                and messages[index + 1].role == "assistant"
            ):
                # 读取对应助手回答的资料范围和来源。
                details = messages[index + 1].details or {}
            # 把回答详情转换成可实时复核的依赖结构。
            deps = self.dependencies(session, details)
            # 用户原话需要与模型回答引用的记忆区别处理。
            if message.role == "user":
                # 复制来源依赖，调整用户陈述的记忆依赖字段。
                deps = {
                    # 保留知识库、文档与附件等原有来源信息。
                    **deps,
                    # 用户明确陈述不自动继承模型使用过的记忆依赖。
                    "memories": [],
                }  # A user's explicit statement is not a model-generated assertion.
            # 助手回答有证据来源时，额外保存可回读资料。
            if message.role == "assistant" and details.get("sources"):
                # 建立加密资料实体，保存本轮获准使用的原始来源。
                artifact = MemoryArtifact(
                    # 为资料生成独立 ID。
                    id=new_id(),
                    # 资料仅属于当前会话用户。
                    owner_id=conversation.user_id,
                    # 关联原始会话，便于追溯其产生位置。
                    conversation_id=conversation.id,
                    # 对来源列表计算内容指纹。
                    digest=digest(details["sources"]),
                    # 资料正文和依赖一起保存，回读时仍需验证授权。
                    payload={"sources": details["sources"], "deps": deps},
                )
                # 把资料加入当前事务，与消息事件一并提交。
                session.add(artifact)
                # 生成共享的调用 ID，将随后两个合成事件配成一对。
                call_id = new_id()
                # 将已获准的检索证据表达为调用与结果事件；不是执行或重放任意工具。
                for kind, role, content in (
                    (
                        # 第一个事件标记为工具调用。
                        "tool_call",
                        # 调用由助手角色发起。
                        "assistant",
                        # 使用固定工具名描述读取已审查证据的动作。
                        {"text": "读取本轮已通过最终审查的证据", "name": "rag_evidence"},
                    ),
                    (
                        # 第二个事件标记为工具结果。
                        "tool_result",
                        # 结果使用 tool 角色。
                        "tool",
                        {
                            # 保存本轮证据正文，作为原始资料回读内容。
                            "text": "\n\n".join(s["text"] for s in details["sources"]),
                            # 提供可在历史投影中保留的轻量 artifact 引用。
                            "artifact_ref": "artifact://" + artifact.id,
                            # 引用携带资料正文指纹。
                            "digest": artifact.digest,
                            # 记录来源数量，方便摘要说明与调试。
                            "count": len(details["sources"]),
                            # 检索片段不自动视为独立核实过的事实，因此明确标记未验证。
                            "verified": False,
                        },
                    ),
                ):
                    # 为每个合成事件递增会话序号。
                    sequence += 1
                    # 将该事件加入当前事务。
                    session.add(
                        # 构造持久化的工具调用或结果事件。
                        MemoryEvent(
                            # 事件归属与会话用户一致。
                            owner_id=conversation.user_id,
                            # 绑定原始会话。
                            conversation_id=conversation.id,
                            # 关联生成该证据的助手消息。
                            message_id=message.id,
                            # 使用刚分配的顺序号。
                            sequence=sequence,
                            # 填入当前合成事件类型。
                            kind=kind,
                            # 填入与事件类型对应的角色。
                            role=role,
                            # 两个合成事件共用调用 ID，以便安全切分时配对。
                            tool_call_id=call_id,
                            # 正文同时携带完整来源依赖。
                            content={**content, "deps": deps},
                            # 沿用原消息时间，不把同步时间误当事件发生时间。
                            created_at=message.created_at,
                        )
                    )
            # 再为原始用户或助手消息分配一个序号。
            sequence += 1
            # 将原始消息事件加入同一事务。
            session.add(
                # 构造可用于摘要和记忆提炼的原始消息事件。
                MemoryEvent(
                    # 保持个人归属隔离。
                    owner_id=conversation.user_id,
                    # 记录所属会话。
                    conversation_id=conversation.id,
                    # 指向原始聊天消息 ID。
                    message_id=message.id,
                    # 使用连续事件序号保证回放顺序。
                    sequence=sequence,
                    # 沿用原消息角色。
                    role=message.role,
                    # 将 user / assistant 角色转换成对应消息事件类型。
                    kind=message.role + "_message",
                    # 原文与其资源依赖共同持久化。
                    content={"text": message.content, "deps": deps},
                    # 保留原始消息创建时间。
                    created_at=message.created_at,
                )
            )
        # 刷入事务内状态，后续快照查询立即可见这些事件。
        session.flush()

    # 读取受当前权限和范围约束的工作上下文快照。
    def snapshot(self, cid, uid):
        # 同步事件与读取状态在同一事务中执行。
        with self.db.session.begin() as session:
            # 先通过现有仓储验证会话归属。
            conversation = self.repository._owned(session, cid, uid)
            # 锁定用户记忆状态，使首次初始化及快照过程一致。
            state = self.state(session, uid, lock=True)
            # 将已提交但未投影的消息补入事件表。
            self.sync_events(session, conversation)
            # 取得当前选库、附件和历史起点。
            scope = self.scope(conversation)
            # 查询当前范围允许纳入历史的原始事件。
            raw = session.scalars(
                # 以事件表为读取源。
                select(MemoryEvent)
                # 限定会话和范围切换后的消息边界。
                .where(
                    # 只选择本会话且晚于 scope_start_id 的消息事件。
                    MemoryEvent.conversation_id == cid,
                    # 范围切换前的消息事件不再进入当前上下文。
                    MemoryEvent.message_id > conversation.scope_start_id,
                )
                # 按会话序号保持原始发生顺序。
                .order_by(MemoryEvent.sequence)
                # 把查询结果读入当前事务。
            ).all()
            # Filter entire turns, not individual tool messages, so pairing never breaks after revocation.
            # 先按完整回合分段，再做授权过滤，避免删掉单条工具消息破坏配对。
            segments, open_tail = closed_segments([event_dict(e) for e in raw])
            # 存在未闭合尾部时仍需保留并做权限检查。
            if open_tail:
                # 尾部只能保留原文，不因此成为可压缩回合。
                segments.append(open_tail)  # 未闭合后缀保留原文并接受审查，但不会进入压缩切点。
            # 构建只包含当前可访问回合的事件列表。
            events = [
                # 输出每个通过授权的回合中的原始事件。
                e
                # 逐个检查完整回合或未闭合尾部。
                for segment in segments
                # 只有回合内每个事件的依赖都有效，才保留整个回合。
                if all(self.accessible(session, uid, e["content"].get("deps", {}), scope) for e in segment)
                # 按原顺序展平获准回合。
                for e in segment
            ]
            # 读取会话当前选中的检查点。
            checkpoint = (
                # 有活动检查点 ID 时查询其数据库记录。
                session.get(CompactionCheckpoint, conversation.active_checkpoint_id)
                # 仅在活动指针非空时访问检查点表。
                if conversation.active_checkpoint_id
                # 无活动检查点时使用原始历史投影。
                else None
            )
            # 计算当前范围的稳定指纹。
            scope_key = digest(scope)
            # 活动检查点存在时还需验证其适用范围和原始来源。
            if checkpoint and (
                # 范围指纹不同说明检查点基于旧选库或旧附件范围。
                checkpoint.scope_key != scope_key
                # 对仍可访问且被该检查点覆盖的事件重新计算指纹。
                or digest([e for e in events if e["id"] in checkpoint.source_event_ids])
                # 指纹不一致表明来源内容或可见性已经变化。
                != checkpoint.source_digest
            ):
                # 本次快照不再使用失效检查点。
                checkpoint = None
            # 把有效检查点转换成与 ORM 生命周期无关的数据。
            cp = (
                # 无有效检查点时输出 None。
                None
                # 按前面的来源校验结果选择分支。
                if checkpoint is None
                # 有检查点时返回其摘要和追溯字段。
                else {
                    # 保留检查点 ID，供恢复和 UI 展示。
                    "id": checkpoint.id,
                    # 记录摘要覆盖的最后一个事件序号。
                    "end_seq": checkpoint.end_seq,
                    # 提供结构化工作摘要。
                    "summary": checkpoint.summary,
                    # 保存摘要所依赖的原始事件 ID。
                    "source_event_ids": checkpoint.source_event_ids,
                    # 保留父检查点，支持版本追溯。
                    "parent_id": checkpoint.parent_id,
                    # 暴露该次压缩前的输入成本。
                    "tokens_before": checkpoint.tokens_before,
                    # 暴露压缩后的输入成本。
                    "tokens_after": checkpoint.tokens_after,
                    # 保留检查点创建时间。
                    "created_at": checkpoint.created_at,
                }
            )
            # 返回上下文服务所需的一致快照。
            return {
                # 标识会话。
                "conversation_id": cid,
                # 标识所属用户。
                "owner_id": uid,
                # 上下文版本用于后续 CAS 提交。
                "revision": conversation.context_revision,
                # 完整范围用于权限和记忆检索限制。
                "scope": scope,
                # 范围指纹用于快速识别选择变化。
                "scope_key": scope_key,
                # 仅包含通过整回合来源过滤的事件。
                "events": events,
                # 活动检查点可能因来源变化被降为 None。
                "checkpoint": cp,
                # 复制固定原文 ID 列表，避免把 ORM 容器直接交给异步处理。
                "pins": list(conversation.context_pins),
                # 保存个人记忆状态版本，供后台任务失效判断。
                "memory_version": state.version,
                # 全局和用户开关都开启时才允许使用记忆。
                "memory_enabled": state.enabled and self.settings.memory_enabled,
                # 自动提炼不得重新读取这一时间边界之前的已遗忘历史。
                "forget_before": state.forget_before,
            }

    # 最终回答提交前复核整轮实际使用过的所有上下文来源。
    def validate_context(self, session, uid, report, scope):
        # 重新检查账号是否仍启用、角色是否仍有问答权限。
        self.state(session, uid)  # Recheck live account/role permission at commit time.
        # Final commit validates every event actually supplied, including old compressed evidence.
        # 使用过历史事件且其中有来源失效时，拒绝保存本轮结果。
        if not self.evidence_accessible(session, uid, report.get("event_ids", []), scope) and report.get(
            # 空历史不触发缺少证据错误。
            "event_ids"
        ):
            # 明确报告生成过程中历史来源发生变化。
            raise AppError("context_source_changed", "生成期间历史来源权限或版本已变化", 409)
        # 逐条复核报告记录的记忆，包括中途从后续提示中移除的记忆。
        for mid in report.get("memory_ids", []):
            # 刷新用户记忆设置，捕获生成期间关闭记忆的操作。
            state = session.get(MemoryState, uid, populate_existing=True)
            # 设置和记忆状态必须仍然有效。
            if (
                # 不存在状态行时拒绝复用。
                state is None
                # 用户关闭记忆时不再提交基于记忆的结果。
                or not state.enabled
                # 同时检查该记忆的当前版本状态、到期和原始证据。
                or not self.record_accessible(
                    # 强制读取当前记忆记录，并限制在本轮范围内。
                    session,
                    # 使用当前提交者身份检查记忆。
                    uid,
                    # 强制刷新目标记忆记录，避免忽略生成期间的修改。
                    session.get(MemoryRecord, mid, populate_existing=True),
                    # 继续限制在本轮问答的资料范围内。
                    scope,
                )
            ):
                # 删除、更正或撤权均要求重新生成。
                raise AppError("memory_changed", "生成期间记忆已删除、更正或失效，请重试", 409)
            # 只有通过最终验证的使用才更新最近使用时间。
            session.get(MemoryRecord, mid).last_used_at = time.time()

    # 原子保存压缩检查点，并用版本、租约和内容指纹防止并发覆盖。
    def commit_checkpoint(self, snapshot, summary, covered, before, after, token):
        # 检查与写入共享一个事务，失败会整体回滚。
        with self.db.session.begin() as session:
            # 再次核实会话属于快照中的用户。
            row = self.repository._owned(session, snapshot["conversation_id"], snapshot["owner_id"])
            # 通过条件更新争取本次快照的提交资格。
            changed = session.execute(
                # 更新目标是当前会话行。
                update(Conversation)
                # 以下条件共同构成 CAS 与租约校验。
                .where(
                    # 必须命中同一会话。
                    Conversation.id == row.id,
                    # 上下文版本必须与压缩开始时相同。
                    Conversation.context_revision == snapshot["revision"],
                    # 当前会话锁必须仍由本轮令牌持有。
                    Conversation.lock_token == token,
                    # 租约必须尚未过期。
                    Conversation.lock_until > time.time(),
                )
                # 成功命中时把上下文版本加一。
                .values(context_revision=Conversation.context_revision + 1)
            )
            # 没有更新资格或会话范围已经改变，都视为快照过期。
            if not changed.rowcount or digest(self.scope(row)) != snapshot["scope_key"]:
                # 抛错回滚，不让过时摘要覆盖新的会话状态。
                raise AppError("context_changed", "会话上下文已变化，请重试压缩", 409)
            # 提交前再次检查摘要引用的全部原始事件。
            if not self.evidence_accessible(
                # 使用会话真实用户、覆盖事件 ID 和原始快照范围。
                session,
                # 使用会话真实所属用户作为授权主体。
                row.user_id,
                # 检查摘要覆盖的全部事件 ID。
                [e["id"] for e in covered],
                # 坚持压缩开始时的资料范围，不随模型输出改变。
                snapshot["scope"],
            ):
                # 来源权限或版本变化时拒绝该摘要。
                raise AppError("context_changed", "摘要来源权限或版本已变化", 409)
            # 从数据库重新读取覆盖事件，避免只依赖内存副本。
            current_events = [
                # 强制刷新事件实体并转换成同一指纹格式。
                event_dict(session.get(MemoryEvent, e["id"], populate_existing=True))
                # 遍历压缩候选声称覆盖的所有原始事件。
                for e in covered
            ]
            # 比较当前数据库事件与摘要生成时读取的事件内容。
            if digest(current_events) != digest(covered):
                # 原文发生变化时不提交旧摘要。
                raise AppError("context_changed", "原始事件内容已变化，摘要未提交", 409)
            # 所有条件通过后创建持久化检查点。
            cp = CompactionCheckpoint(
                # 生成检查点 ID。
                id=new_id(),
                # 设置检查点归属用户。
                owner_id=row.user_id,
                # 关联目标会话。
                conversation_id=row.id,
                # 绑定快照中的资料范围指纹。
                scope_key=snapshot["scope_key"],
                # 记录所覆盖原始事件的起始序号。
                start_seq=covered[0]["sequence"],
                # 记录覆盖结束序号，后续历史从此位置之后继续。
                end_seq=covered[-1]["sequence"],
                # 保留生成摘要时所依赖的上下文版本。
                base_revision=snapshot["revision"],
                # 保存全部来源事件 ID，而非只保留本次新增片段。
                source_event_ids=[e["id"] for e in covered],
                # 保存覆盖事件的完整内容指纹。
                source_digest=digest(covered),
                # 连接旧活动检查点，形成可追溯的检查点链。
                parent_id=(snapshot["checkpoint"] or {}).get("id"),
                # 写入已通过各项审查的结构化摘要。
                summary=summary,
                # 记录压缩前请求成本。
                tokens_before=before,
                # 记录压缩后请求成本。
                tokens_after=after,
                # 记录模型名称，演示模式以 demo 标识。
                model=self.settings.llm_model or "demo",
            )
            # 将新检查点纳入当前事务。
            session.add(cp)
            # 在同一事务内切换活动指针。
            row.active_checkpoint_id = cp.id
            # 返回新 ID，事务成功退出后才真正对外提交。
            return cp.id

    # 在问答最终提交事务内同步事件，并调度空闲后的记忆提炼。
    def on_finish(self, session, conversation):
        # 读取当前用户记忆设置并检查账号权限。
        state = self.state(session, conversation.user_id)
        # 将刚提交的用户和助手消息纳入原始事件。
        self.sync_events(session, conversation)
        # 推进会话上下文版本，使旧快照和旧任务可被识别。
        conversation.context_revision += 1
        # 刷新事务状态，后面的计数和任务查询看到最新消息。
        session.flush()
        # 任一全局、个人或自动提炼开关关闭时不创建新任务。
        if not self.settings.memory_enabled or not state.enabled or not state.auto_extract:
            # 只完成事件同步与版本更新。
            return
        # 统计该会话已提交消息总量。
        count = session.scalar(
            # 计数仅针对当前会话的消息表记录。
            select(func.count()).select_from(Message).where(Message.conversation_id == conversation.id)
        )
        # 以每回合两条消息近似判断是否达到最小提炼轮数。
        if count < self.settings.memory_min_turns * 2:
            # 历史还太短时暂不提炼。
            return
        # Coalesce idle extraction work; a long conversation must not leave one costly job per turn.
        # 合并尚未运行的旧任务，避免长会话每一轮都积压一次模型调用。
        session.execute(
            # 更新后台记忆任务表。
            update(MemoryJob)
            # 只淘汰当前会话 pending 或 retry 的任务，不抢占运行中任务。
            .where(MemoryJob.conversation_id == conversation.id, MemoryJob.status.in_(["pending", "retry"]))
            # 把旧任务置为 obsolete，并清除其租约信息。
            .values(status="obsolete", lease_token=None, lease_until=0)
        )
        # 按会话 ID、上下文版本和记忆版本生成确定性任务 ID。
        job_id = digest([conversation.id, conversation.context_revision, state.version])[:32]
        # 相同状态下已经存在任务就不重复创建。
        if session.get(MemoryJob, job_id) is None:
            # 将新的持久任务加入问答提交事务。
            session.add(
                # 构造后台提炼任务。
                MemoryJob(
                    # 使用确定性 ID 保证重复提交幂等。
                    id=job_id,
                    # 绑定当前用户。
                    owner_id=conversation.user_id,
                    # 绑定当前会话。
                    conversation_id=conversation.id,
                    # 保存来源会话版本，后续改变会使任务失效。
                    revision=conversation.context_revision,
                    # 保存个人记忆版本，遗忘或设置改变后旧任务不能提交。
                    memory_version=state.version,
                    # 任务初始状态为等待执行。
                    status="pending",
                    # 从候选提炼阶段开始。
                    stage="extract",
                    # 安排在配置的空闲等待时间之后执行。
                    run_after=time.time() + self.settings.memory_idle_seconds,
                )
            )

    # 读取个人记忆；默认仅返回可用于作答的 active 记录。
    def list_records(self, uid, scope=None, *, active_only=True):
        # 使用事务以兼容首次用户状态初始化。
        with self.db.session.begin() as session:
            # 验证账号和角色，并读取个人记忆开关。
            state = self.state(session, uid)
            # 构造当前用户的记忆列表查询。
            query = (
                # 从记忆版本记录表读取。
                select(MemoryRecord)
                # 强制按 owner_id 限制，避免跨用户枚举。
                .where(MemoryRecord.owner_id == uid)
                # 优先展示最近更新的记录。
                .order_by(MemoryRecord.updated_at.desc())
            )
            # 读取匹配的所有记录，再逐条做来源校验。
            rows = session.scalars(query).all()
            # 收集满足调用模式的结果。
            output = []
            # 逐条检查状态、有效期和证据。
            for row in rows:
                # 用于检索时只接纳启用且可访问的记录。
                if active_only and (
                    # 个人开关或任一来源检查失败都不能进入检索集合。
                    not state.enabled or not self.record_accessible(session, uid, row, scope)
                ):
                    # 跳过不可用于作答的记录。
                    continue
                # 管理列表允许非 active 状态，但仍不能展示无权访问的正文。
                if (
                    # 以下分支只作用于管理查看模式。
                    not active_only
                    # 删除墓碑本身可保留用于管理展示。
                    and row.status != "deleted"
                    # 非删除记录必须仍具有可访问的原始证据。
                    and not self.evidence_accessible(session, uid, row.evidence_ids)
                ):
                    # 跳过来源已经不可访问的历史版本或争议记录。
                    continue
                # 把可返回记录转换成普通字典。
                output.append(
                    {
                        # 记忆版本记录 ID。
                        "id": row.id,
                        # 关联知识库范围，个人通用记忆可以为 None。
                        "base_id": row.base_id,
                        # 内部稳定 HMAC 键；服务层管理接口会进一步去掉它。
                        "canonical_key": row.canonical_key,
                        # 事实、偏好、决定或经历类别。
                        "category": row.category,
                        # 生命周期状态。
                        "status": row.status,
                        # 同一稳定键下的版本号。
                        "version": row.version,
                        # 经字段类型解密后的业务内容。
                        "payload": row.payload,
                        # 原始事件证据引用。
                        "evidence_ids": row.evidence_ids,
                        # 内部检索使用的向量，不直接暴露给管理 UI。
                        "embedding": row.embedding,
                        # 记录向量所属模型空间。
                        "embedding_signature": row.embedding_signature,
                        # 候选效用得分。
                        "utility": row.utility,
                        # 内容置信度。
                        "confidence": row.confidence,
                        # 首次创建时间。
                        "created_at": row.created_at,
                        # 最近更新或证据合并时间。
                        "updated_at": row.updated_at,
                        # 最近一次最终提交成功的使用时间。
                        "last_used_at": row.last_used_at,
                        # 可选的到期时间。
                        "expires_at": row.expires_at,
                    }
                )
            # 返回当前模式下获准读取的记忆集合。
            return output

    # 在异步模型操作之后，重新检查一组记忆引用是否仍有效。
    def validate_references(self, uid, ids, scope=None):
        # 使用独立只读会话，获取最新持久状态。
        with self.db.session() as session:
            # 刷新账号记录。
            user = session.get(User, uid, populate_existing=True)
            # 账号必须存在、启用，并仍具有 chat.use 权限。
            if not user or not user.active or "chat.use" not in self.security.permissions(session, user.role):
                # 权限失效时直接拒绝整组引用。
                return False
            # 读取个人记忆开关。
            state = session.get(MemoryState, uid)
            # 存在记忆引用时，要求记忆状态存在且处于开启状态。
            if ids and (state is None or not state.enabled):
                # 关闭记忆后不能继续使用先前召回结果。
                return False
            # 所有引用都通过才能返回 True。
            return all(
                # 逐条验证记忆本身及原始来源。
                self.record_accessible(
                    # 刷新对应记录，并传入本轮资料范围。
                    session,
                    # 按当前请求的用户身份验证。
                    uid,
                    # 从数据库刷新当前记忆版本及状态。
                    session.get(MemoryRecord, mid, populate_existing=True),
                    # 校验时遵守会话选库和附件范围。
                    scope,
                )
                # 遍历待检查的记忆 ID。
                for mid in ids
            )

    # 按授权回读记忆或摘要的原始事件证据。
    def read_events(self, uid, ids):
        # 在数据库会话中完成来源复核和读取。
        with self.db.session() as session:
            # 任何事件不可用时都不返回部分证据。
            if not self.evidence_accessible(session, uid, ids):
                # 统一提示来源不存在或授权变化。
                raise AppError("memory_source_unavailable", "记忆来源不存在或权限已变化", 404)
            # 保持传入 ID 顺序，返回规范化事件字典。
            return [event_dict(session.get(MemoryEvent, eid)) for eid in ids]

    # 按资料 ID 回读历史证据正文。
    def artifact(self, uid, aid):
        # 在读取正文前执行实时权限检查。
        with self.db.session() as session:
            # 查询存储的证据资料。
            row = session.get(MemoryArtifact, aid)
            # 资料必须存在、属于当前用户且全部依赖仍然有效。
            if row is None or row.owner_id != uid or not self.accessible(session, uid, row.payload["deps"]):
                # 用统一的不可访问错误避免泄露其他用户资料是否存在。
                raise AppError("not_found", "资料不存在或访问权限已变化", 404)
            # 只返回资料 ID、指纹和获准回读的来源正文。
            return {"id": row.id, "digest": row.digest, "sources": row.payload["sources"]}

    # 更新本会话固定原文列表，使后续压缩不能跨过这些用户消息。
    def set_pins(self, uid, cid, ids):
        # 先取得会话租约，与并发问答或压缩操作协调。
        token = self.repository.claim(cid, uid)
        # 无论写入结果如何，都必须在 finally 中释放租约。
        try:
            # 固定列表和上下文版本在同一事务内更新。
            with self.db.session.begin() as session:
                # 再次确认会话归属。
                row = self.repository._owned(session, cid, uid)
                # 非空固定列表必须全部能被当前用户访问。
                if not self.evidence_accessible(session, uid, ids) and ids:
                    # 无效事件不允许写入固定列表。
                    raise AppError("not_found", "固定消息不存在", 404)
                # 逐个检查拟固定事件的会话和类型。
                for eid in ids:
                    # 读取原始事件记录。
                    event = session.get(MemoryEvent, eid)
                    # 仅能固定本会话的 user_message，不能固定助手推断或其他会话内容。
                    if event.conversation_id != cid or event.kind != "user_message":
                        # 对非法固定请求返回字段语义错误。
                        raise AppError("invalid_pin", "仅能固定本会话的用户原始消息", 422)
                # 按首次出现顺序去重后保存固定 ID。
                row.context_pins = list(dict.fromkeys(ids))
                # 取消旧活动摘要，避免它已压缩了现在要求保留的原文。
                row.active_checkpoint_id = None
                # 推进版本，让正在运行的旧快照无法提交。
                row.context_revision += 1
        # 即使校验或事务失败也执行租约清理。
        finally:
            # 仅用自己取得的令牌释放会话锁。
            self.repository.release(cid, token)

    # 恢复指定检查点；传 None 时恢复原始历史投影。
    def restore(self, uid, cid, checkpoint_id):
        # 先获取会话租约，阻止与生成或压缩并发改写。
        token = self.repository.claim(cid, uid)
        # 确保恢复过程中异常也会释放锁。
        try:
            # 取得当前授权事件与范围指纹。
            snapshot = self.snapshot(cid, uid)
            # 在事务内检查目标并切换活动检查点。
            with self.db.session.begin() as session:
                # 再次校验会话归属。
                row = self.repository._owned(session, cid, uid)
                # 非空 ID 才需要读取及验证旧检查点。
                if checkpoint_id:
                    # 查询待恢复的检查点。
                    cp = session.get(CompactionCheckpoint, checkpoint_id)
                    # 目标必须同时满足身份、范围和来源完整性。
                    if (
                        # 不存在的检查点不可恢复。
                        cp is None
                        # 禁止跨用户恢复。
                        or cp.owner_id != uid
                        # 禁止把其他会话的摘要装入当前会话。
                        or cp.conversation_id != cid
                        # 旧检查点必须对应当前资料范围。
                        or cp.scope_key != snapshot["scope_key"]
                        # 基于当前可访问事件重新计算该检查点来源指纹。
                        or digest([e for e in snapshot["events"] if e["id"] in cp.source_event_ids])
                        # 来源指纹必须与保存时一致。
                        != cp.source_digest
                    ):
                        # 条件不满足时拒绝切换活动指针。
                        raise AppError("checkpoint_unavailable", "检查点来源已失效或不属于当前会话范围", 409)
                    # 检查目标摘要是否覆盖了用户当前固定的原文。
                    if set(row.context_pins) & set(cp.source_event_ids):
                        # 发现冲突时要求先调整固定策略。
                        raise AppError(
                            # 明确说明检查点与固定原文不可同时满足的原因。
                            "checkpoint_pinned",
                            # 提示先取消固定或保留原始投影，避免原文被摘要替代。
                            "该检查点覆盖固定原文，请先取消固定或保留原始投影",
                            # 以 409 表示检查点与当前固定状态冲突。
                            409,
                        )
                # 切换到指定检查点，或清空指针以恢复原始投影。
                row.active_checkpoint_id = checkpoint_id
                # 推进上下文版本，使其他快照失效。
                row.context_revision += 1
                # 返回实际选择的检查点 ID。
                return {"checkpoint_id": checkpoint_id}
        # 成功或失败都需要释放恢复操作持有的会话租约。
        finally:
            # 使用本次令牌释放锁。
            self.repository.release(cid, token)

    # 读取或更新个人记忆及自动提炼开关。
    def preferences(self, uid, changes=None):
        # 设置更新与版本递增在同一事务内完成。
        with self.db.session.begin() as session:
            # 对当前用户串行化记忆状态写入。
            state = self.state(session, uid, lock=True)
            # 传入变更时才执行设置更新。
            if changes:
                # 只接纳两个明确允许修改的设置字段。
                for key in ("enabled", "auto_extract"):
                    # 未提供的字段保持原状。
                    if key in changes:
                        # 将提供的布尔设置写入状态实体。
                        setattr(state, key, changes[key])
                # 推进记忆版本，让基于旧设置的后台任务无法提交。
                state.version += 1
            # 返回最新开关与版本，供前端状态刷新。
            return {"enabled": state.enabled, "auto_extract": state.auto_extract, "version": state.version}

    # 遗忘指定稳定键的全部版本，或清空当前用户的全部记忆。
    def forget(self, uid, mid=None):
        # 记忆清理、任务取消和检查点失效一起事务提交。
        with self.db.session.begin() as session:
            # 锁定用户状态，防止后台整合与遗忘交叉写入。
            state = self.state(session, uid, lock=True)
            # 始终从当前用户的记忆集合开始过滤。
            query = select(MemoryRecord).where(MemoryRecord.owner_id == uid)
            # 提供单条 ID 时定位其所属稳定键。
            if mid:
                # 读取目标版本记录。
                target = session.get(MemoryRecord, mid)
                # 目标必须存在且属于当前用户。
                if not target or target.owner_id != uid:
                    # 对无权操作的目标返回不存在。
                    raise AppError("not_found", "记忆不存在", 404)
                # 删除同一稳定键的所有版本，防止旧版本再次被检索。
                query = query.where(MemoryRecord.canonical_key == target.canonical_key)
            # 未指定 ID 表示执行全部遗忘。
            else:
                # 记录时间边界，阻止自动提炼重新使用边界之前的原话。
                state.forget_before = time.time()
            # 遍历本次需要遗忘的全部版本记录。
            for row in session.scalars(query):
                # 保留删除墓碑，但清空业务正文与向量。
                row.status, row.payload, row.embedding = "deleted", {}, None
                # 记录遗忘发生时间。
                row.updated_at = time.time()
            # 推进记忆版本，使先前领取的后台任务失效。
            state.version += 1
            # Remove pending derived text and invalidate worker fencing; raw approved history remains intact.
            # 删除当前用户所有尚存候选，避免候选正文在遗忘后留存。
            session.execute(delete(MemoryCandidate).where(MemoryCandidate.owner_id == uid))
            # 取消可能继续写入旧知识的后台任务。
            session.execute(
                # 更新记忆任务表。
                update(MemoryJob)
                # 只处理当前用户等待、运行和重试中的任务。
                .where(MemoryJob.owner_id == uid, MemoryJob.status.in_(["pending", "running", "retry"]))
                # 撤销运行状态及租约，旧 worker 随后会被 fencing 校验拒绝。
                .values(status="cancelled", lease_token=None, lease_until=0)
            )
            # 让当前用户所有会话的活动摘要失效。
            session.execute(
                # 更新会话表中的派生上下文指针。
                update(Conversation)
                # 严格限定为该用户的会话。
                .where(Conversation.user_id == uid)
                # 清空检查点并递增版本；原始获准历史消息仍单独保留。
                .values(active_checkpoint_id=None, context_revision=Conversation.context_revision + 1)
            )
            # 在同一事务中记录遗忘动作的审计事件。
            self.security.audit("memory.forget", mid or "all", session=session, actor_id=uid)
