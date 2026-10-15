# 编排异步模型调用、后台 worker、任务超时和数据库线程卸载。
import asyncio

# 关闭 worker 时只抑制预期的取消异常。
import contextlib

# 序列化记忆内容，用于稳定键、安全审查和原始指令记录。
import json

# 识别引文中的疑问或假设表达，避免自动提炼成确定事实。
import re

# 计算有效期、任务租约、重试退避和更新时间。
import time

# 构造计数、读取和带条件的原子更新。
from sqlalchemy import func, select, update

# 在后台任务中绑定操作用户，使审计与权限检查使用正确身份。
from app.common.context import actor

# 统一报告权限、版本、任务过期和模型协议等业务错误。
from app.common.errors import AppError

# 复用指纹、文本规范化、敏感信息检查和效用评分规则。
from app.memory.policy import digest, normalized, sensitive, utility

# 复用向量配置签名与记忆文本表示，保持索引与检索口径一致。
from app.memory.retrieval import embedding_signature, render

# 导入会话、提炼候选、证据事件、任务、版本化记忆及 ID 生成器。
from app.models.entities import Conversation, MemoryCandidate, MemoryEvent, MemoryJob, MemoryRecord, new_id


# 编排手动记忆、自动提炼、语义整合和持久任务生命周期。
class MemoryService:
    # 注入共享依赖，服务本身不额外创建模型客户端或数据库。
    def __init__(self, settings, store, llm, embedder, retriever, moderation, tracing):
        # 保存配置、存储、LLM 和 Embedding 服务。
        self.settings, self.store, self.llm, self.embedder = settings, store, llm, embedder
        # 保存检索、内容审查和追踪服务。
        self.retriever, self.moderation, self.tracing = retriever, moderation, tracing
        # 后台任务尚未启动时用 None 表示。
        self.worker = None

    # 为同一知识库范围、主体与属性生成私有稳定键。
    def key(self, base_id, subject, predicate):
        # 使用加密组件的盲索引 HMAC，避免把明文事实键直接存入索引。
        return self.store.db.cipher.blind(
            # 规范化主体和属性并加入库范围，保持同义格式稳定和范围隔离。
            json.dumps([base_id, normalized(subject), normalized(predicate)], ensure_ascii=False),
            # 使用 memory-key 域分离，避免和其他盲索引用途混用。
            "memory-key",
        )

    # 接收用户明确记住或更正的内容，模型调用前后均进行授权检查。
    async def manual(self, uid, value, *, memory_id=None, expected_version=None):
        # Resolve ownership before an external embedding call, then recheck inside the write transaction.
        # 定义同步预授权逻辑，后续在线程中执行数据库访问。
        def authorize():
            # 使用只读会话检查外部调用之前的资源归属。
            with self.store.db.session() as session:
                # 关联知识库必须属于当前用户可见范围。
                if value.get("base_id") and value["base_id"] not in self.store.visible_bases(session, uid):
                    # 未授权知识库不进入 Embedding 调用。
                    raise AppError("not_found", "知识库不存在或无权限", 404)
                # 更正已有记忆时还需核对目标记录归属。
                if memory_id:
                    # 查询用户要求更正的记忆版本。
                    row = session.get(MemoryRecord, memory_id)
                    # 不存在或属于其他用户都不能操作。
                    if row is None or row.owner_id != uid:
                        # 统一返回不存在，避免泄露他人的记忆 ID。
                        raise AppError("not_found", "记忆不存在", 404)

        # 在线程中完成预授权，不阻塞事件循环。
        await asyncio.to_thread(authorize)
        # 凭据、身份等敏感信息不适合进入长期记忆。
        if sensitive(value):
            # 提示用户移除敏感内容后再保存。
            raise AppError("memory_sensitive", "记忆不接受凭据、联系方式或身份敏感信息，请移除后重试", 422)
        # 对完整手工输入执行安全审查。
        await self.moderation.guard(json.dumps(value, ensure_ascii=False), "input")
        # 构造临时记忆表示，供向量化时生成统一可读文本。
        record = {"payload": value, "category": value["category"], "version": 1, "updated_at": time.time()}
        # 根据向量开关决定是否生成 Embedding。
        vector = (
            # 启用时只向量化本条记忆；关闭时保存 None。
            (await self.embedder.embed([render(record)]))[0] if self.settings.memory_vector_enabled else None
        )
        # 模型操作结束后进入数据库事务，再次核对权限和版本。
        return await asyncio.to_thread(self._manual, uid, value, vector, memory_id, expected_version)

    # 在单一事务内创建原始指令事件并保存新的记忆版本。
    def _manual(self, uid, value, vector, memory_id, expected_version):
        # 所有状态修改与审计写入一起提交或回滚。
        with self.store.db.session.begin() as session:
            # 锁定用户记忆状态，串行化手工保存、纠错和遗忘操作。
            state = self.store.state(session, uid, lock=True)
            # 读取这条记忆的可选知识库范围。
            base_id = value.get("base_id")
            # 外部 Embedding 期间权限可能变化，因此再次复查 ACL。
            if base_id and base_id not in self.store.visible_bases(session, uid):
                # 已失去库授权时不写入任何内容。
                raise AppError("not_found", "知识库不存在或无权限", 404)
            # 计算本条事实的稳定 HMAC 键。
            key = self.key(base_id, value["subject"], value["predicate"])
            # 更正模式需要验证目标身份和不可变字段。
            if memory_id:
                # 刷新待更正的版本记录。
                target = session.get(MemoryRecord, memory_id, populate_existing=True)
                # 再次核实目标存在且归属当前用户。
                if not target or target.owner_id != uid:
                    # 对不可访问目标返回统一错误。
                    raise AppError("not_found", "记忆不存在", 404)
                # 更正只能改变事实值等内容，不能偷换知识库范围或事实身份。
                if target.base_id != base_id or any(
                    # 逐项比较主体和属性与原记录是否一致。
                    target.payload.get(k) != value[k]
                    # 主体和属性共同构成不可变的事实身份。
                    for k in ("subject", "predicate")
                ):
                    # 不可变字段被修改时拒绝以纠错方式写入。
                    raise AppError(
                        # 提示此类新事实应走创建入口。
                        "memory_key_changed",
                        # 提示更正需沿用身份字段，新事实应另行创建。
                        "更正时保留原主体、属性和知识库范围；其他事实请新建",
                        # 以 422 表示请求内容不符合更正约束。
                        422,
                    )
                # 继续沿用目标稳定键，保留版本链身份。
                key = target.canonical_key
            # 读取该用户同一稳定键的全部历史版本。
            rows = session.scalars(
                # 同时限制用户归属和事实键，防止跨用户合并。
                select(MemoryRecord).where(MemoryRecord.owner_id == uid, MemoryRecord.canonical_key == key)
                # 取出版本列表，供乐观并发检查和版本递增。
            ).all()
            # 更正模式要求客户端提交预期版本。
            if memory_id:
                # 目标版本必须等于预期值且仍是该稳定键的最新版本。
                if target.version != expected_version or target.version != max(r.version for r in rows):
                    # 并发更正或目标已过时时要求刷新重试。
                    raise AppError("memory_changed", "记忆版本已变化，请刷新后重试", 409)
            # 计算用户当前 active 与 disputed 记录总数。
            live_count = session.scalar(
                # 使用数据库计数，避免把记录正文读入内存。
                select(func.count())
                # 以记忆版本表作为计数来源。
                .select_from(MemoryRecord)
                # 只计算当前用户会参与有效或争议管理的记录。
                .where(MemoryRecord.owner_id == uid, MemoryRecord.status.in_(["active", "disputed"]))
            )
            # 全新稳定键超过配额时拒绝；已有版本链走更正替换流程。
            if not rows and live_count >= self.settings.memory_max_records:
                # 提示先清理过时记忆。
                raise AppError("memory_quota", "记忆数量已达上限，请先删除过时记忆", 413)
            # 把用户的显式保存指令记录为原始证据事件。
            event = MemoryEvent(
                # 给手工指令生成独立事件 ID。
                id=new_id(),
                # 事件仅归属当前用户。
                owner_id=uid,
                # 独立记忆指令不强制关联聊天会话。
                conversation_id=None,
                # 它也不对应某条聊天消息。
                message_id=None,
                # 独立指令不占用会话内的顺序号。
                sequence=0,
                # 用 memory_instruction 类型区分自动同步的聊天事件。
                kind="memory_instruction",
                # 声明证据来自用户，而非模型推断。
                role="user",
                # 将原始输入和授权依赖一起保存。
                content={
                    # 完整记录用户明确提供的结构化内容。
                    "text": json.dumps(value, ensure_ascii=False),
                    # 关联库时要求后续仍有库访问权；这里不绑定库内容 revision。
                    "deps": {"base_access": [base_id] if base_id else []},
                },
            )
            # 将证据事件加入当前事务。
            session.add(event)
            # 遍历同一事实键已有的版本。
            for old in rows:
                # 只替代仍有效或处于争议中的旧版本。
                if old.status in {"active", "disputed"}:
                    # 标记为 superseded 并记录更正时间，不直接覆盖原文。
                    old.status, old.updated_at = "superseded", time.time()
            # 创建新的可用记忆版本。
            row = MemoryRecord(
                # 新版本使用新记录 ID。
                id=new_id(),
                # 保持个人归属。
                owner_id=uid,
                # 保存当前授权的知识库范围。
                base_id=base_id,
                # 沿用稳定事实键，连接新旧版本。
                canonical_key=key,
                # 使用用户选择的记忆类别。
                category=value["category"],
                # 用户明确确认后的版本直接置为 active。
                status="active",
                # 在全部历史版本最大值上加一，保证版本单调递增。
                version=max((r.version for r in rows), default=0) + 1,
                # 业务负载只保留用于记忆内容表达的字段。
                payload={
                    # 保存事实主体。
                    "subject": value["subject"],
                    # 保存事实属性。
                    "predicate": value["predicate"],
                    # 保存用户明确确认的值。
                    "value": value["value"],
                    # 标记来源是用户明确陈述。
                    "evidence_type": "user_explicit",
                },
                # 将新版本链接到刚创建的手工指令事件。
                evidence_ids=[event.id],
                # 保存外部调用生成的向量，关闭向量时为 None。
                embedding=vector,
                # 记录向量所对应的模型与端点配置签名。
                embedding_signature=embedding_signature(self.settings),
                # 手工明确指定的记忆采用最高效用值。
                utility=1.0,
                # 这里的 1.0 表示显式确认，不代表对现实世界事实做过独立证明。
                confidence=1.0,
                # 接受用户指定的可选到期时间。
                expires_at=value.get("expires_at"),
            )
            # 把新记忆加入事务。
            session.add(row)
            # 推进个人记忆版本，使先前后台任务快照失效。
            state.version += 1
            # 把用户记住或更正的操作纳入事务审计。
            self.store.security.audit(
                # 按入口选择审计动作，并记录目标 ID 与操作用户。
                "memory.correct" if memory_id else "memory.remember",
                # 以新记忆版本 ID 作为审计目标。
                row.id,
                # 审计写入与记忆变更共用事务。
                session=session,
                # 明确当前用户为操作主体。
                actor_id=uid,
            )
            # 提前刷新实体，确保返回字段与数据库约束一致。
            session.flush()
            # 只返回新 ID、版本和状态，不回传内部向量或盲索引。
            return {"id": row.id, "version": row.version, "status": row.status}

    # 生成记忆管理界面所需的记录列表。
    def listing(self, uid):
        # 允许查看历史和争议状态，存储层仍执行归属及来源校验。
        rows = self.store.list_records(uid, active_only=False)
        # Never expose the searchable vector or private canonical HMAC in the UI.
        # 逐条移除不应暴露给管理界面的内部检索字段。
        return [
            # 剔除向量、空间签名和私有 HMAC 稳定键。
            {k: v for k, v in r.items() if k not in {"embedding", "embedding_signature", "canonical_key"}}
            # 处理已通过存储层可见性筛选的记录。
            for r in rows
        ]

    # 导出当前可用个人记忆的 Markdown 只读投影。
    def export(self, uid):
        # 默认只读取 active、未过期且来源仍有效的记忆。
        rows = self.store.list_records(uid)
        # 在导出头部说明数据库才是权威源，编辑文件不会修改系统记忆。
        lines = ["# Personal Memory Index", "", "此导出是数据库的只读投影；修改请使用记忆管理接口。", ""]
        # 按四种业务类别组织导出内容。
        for category in ("fact", "preference", "decision", "episode"):
            # 为当前类别筛选记录。
            subset = [r for r in rows if r["category"] == category]
            # 只有非空类别才生成小节标题。
            if subset:
                # 追加类别标题和分隔空行。
                lines += ["## " + category, ""]
            # 遍历该类别下的可用记忆。
            for row in subset:
                # 每条记录输出内容和来源引用两行。
                lines += [
                    # 将内嵌换行折叠为空格，保持单条 Markdown 列表结构。
                    "- " + render(row).replace("\n", " "),
                    # 附带原始事件 ID，便于后续证据追溯。
                    "  - Evidence: " + ", ".join(row["evidence_ids"]),
                ]
        # 拼接成可下载的 Markdown 文本。
        return "\n".join(lines)

    # 从持久队列领取一个到期任务，也可接管租约已过期的运行任务。
    def claim(self):
        # 冻结本次扫描使用的时间基准。
        now = time.time()
        # 扫描与条件领取在同一事务中完成。
        with self.store.db.session.begin() as session:
            # 先取得少量候选任务，随后通过条件更新竞争执行权。
            rows = session.scalars(
                # 查询记忆任务表。
                select(MemoryJob)
                # 只考虑满足执行时间和状态的任务。
                .where(
                    # 未到 run_after 的任务继续等待。
                    MemoryJob.run_after <= now,
                    (
                        # 普通等待或重试任务可进入候选集。
                        (MemoryJob.status.in_(["pending", "retry"]))
                        # 运行任务只有租约已过期时才允许接管。
                        | ((MemoryJob.status == "running") & (MemoryJob.lease_until < now))
                    ),
                )
                # 优先执行等待时间更早的任务。
                .order_by(MemoryJob.run_after)
                # 每次扫描最多 20 个候选，限制查询开销。
                .limit(20)
                # 将候选行读入当前事务。
            ).all()
            # 依次尝试领取，成功一条就返回。
            for row in rows:
                # 尝试次数达到上限的任务不再运行。
                if row.attempts >= self.settings.memory_job_attempts:
                    # 标记租约耗尽失败，留下可观察的错误码。
                    row.status, row.error = "failed", "lease_exhausted"
                    # 清除其租约，避免继续被误判为执行中。
                    row.lease_token, row.lease_until = None, 0
                    # 继续尝试其他候选任务。
                    continue
                # 为本次领取生成新的 fencing 令牌。
                token = new_id()
                # 通过条件更新确保并发 worker 只有一个能领取成功。
                changed = session.execute(
                    # 更新当前候选任务。
                    update(MemoryJob)
                    # 更新时再次校验目标、租约和状态。
                    .where(
                        # 严格限定当前候选任务 ID。
                        MemoryJob.id == row.id,
                        # 只有尚无有效租约的任务能被领取。
                        MemoryJob.lease_until < now,
                        # 已完成、取消或淘汰的任务不能重新进入运行态。
                        MemoryJob.status.in_(["pending", "retry", "running"]),
                    )
                    # 写入新的运行状态和租约。
                    .values(
                        # 任务进入 running。
                        status="running",
                        # 保存本 worker 的唯一令牌。
                        lease_token=token,
                        # 租约覆盖配置的执行超时，并额外保留 30 秒收尾余量。
                        lease_until=now + self.settings.memory_job_timeout + 30,
                        # 每次领取，包括超时接管，都消耗一次尝试次数。
                        attempts=MemoryJob.attempts + 1,
                    )
                )
                # 只有实际更新到行的 worker 才取得执行资格。
                if changed.rowcount:
                    # 返回后台执行所需的最小任务快照。
                    return {
                        # 任务 ID。
                        "id": row.id,
                        # 任务所属用户。
                        "owner_id": row.owner_id,
                        # 提炼来源会话。
                        "conversation_id": row.conversation_id,
                        # 领取时任务记录的来源会话版本。
                        "revision": row.revision,
                        # 任务记录的个人记忆状态版本。
                        "memory_version": row.memory_version,
                        # 持久写入前必须再次比对的 fencing 令牌。
                        "token": token,
                    }
        # 本次没有领到任务时返回 None。
        return None

    # 对后台任务提交执行 fencing 校验，拒绝旧 worker 或旧来源快照。
    def fenced(self, session, job):
        # 刷新数据库任务行，避免使用领取时的陈旧实体。
        row = session.get(MemoryJob, job["id"], populate_existing=True)
        # 任务存在性与租约条件必须全部成立。
        if (
            # 已删除任务不允许提交。
            row is None
            # 令牌改变说明任务已被其他 worker 接管或被取消。
            or row.lease_token != job["token"]
            # 只有当前仍处于 running 的任务能继续写入。
            or row.status != "running"
            # 租约过期后旧 worker 即使完成模型调用也不能提交。
            or row.lease_until < time.time()
        ):
            # 用任务过期错误码终止旧执行者。
            raise AppError("memory_job_stale", "记忆任务租约已失效", 409)
        # 锁住用户记忆状态，协调遗忘、更正和后台写入。
        state = self.store.state(session, row.owner_id, lock=True)
        # 刷新来源会话，检查是否已有新回合或范围变化。
        conversation = session.get(Conversation, row.conversation_id, populate_existing=True)
        # 继续校验来源版本和个人功能开关。
        if (
            # 会话被删除时任务失效。
            not conversation
            # 会话上下文版本不一致说明来源快照已过时。
            or conversation.context_revision != row.revision
            # 记忆版本变化说明发生设置修改、手工更正或遗忘等操作。
            or state.version != row.memory_version
            # 关闭个人记忆后不允许自动写入。
            or not state.enabled
            # 关闭自动提炼后同样拒绝旧任务提交。
            or not state.auto_extract
        ):
            # 将以上变化统一视为旧任务失效。
            raise AppError("memory_job_stale", "记忆来源或设置已变化", 409)
        # 返回已验证的任务、状态和会话实体，供同一事务继续写入。
        return row, state, conversation

    # 持久化第一阶段候选，并把任务推进到整合阶段。
    def stage_candidates(self, job, candidates):
        # 候选保存和阶段切换原子提交。
        with self.store.db.session.begin() as session:
            # 写入之前确认当前 worker 的租约及来源版本仍有效。
            row, _, _ = self.fenced(session, job)
            # 逐个保存已经通过来源、安全与效用检查的候选。
            for candidate in candidates:
                # 使用任务 ID 和候选内容生成确定性候选 ID。
                cid = digest([job["id"], candidate])
                # 相同候选已经保存时不重复插入。
                if session.get(MemoryCandidate, cid) is None:
                    # 把新候选加入当前事务。
                    session.add(
                        # 构造第二阶段可恢复处理的持久候选实体。
                        MemoryCandidate(
                            # 确定性 ID 保证重复执行的幂等性。
                            id=cid,
                            # 绑定任务用户，保持个人隔离。
                            owner_id=job["owner_id"],
                            # 关联原始提炼任务。
                            job_id=job["id"],
                            # candidate 表示尚待整合。
                            status="candidate",
                            # 保存经过协议及证据检查的结构化候选内容。
                            payload=candidate,
                        )
                    )
            # 阶段切到 consolidate，失败重试时可直接复用候选。
            row.stage = "consolidate"

    # 读取尚未整合的持久候选，区分未提炼与已提炼空结果。
    def pending(self, job):
        # 使用独立读取会话。
        with self.store.db.session() as session:
            # 查询当前任务阶段。
            row = session.get(MemoryJob, job["id"])
            # 未进入整合阶段表示需要重新执行提炼。
            if row.stage != "consolidate":
                # 用 None 区别于已经完成提炼但没有候选的空列表。
                return None
            # 返回整合阶段尚未处理的候选。
            return [
                # 只提供候选 ID 与业务内容。
                {"id": c.id, "payload": c.payload}
                # 遍历匹配任务的候选查询结果。
                for c in session.scalars(
                    # 从候选表中读取待处理行。
                    select(MemoryCandidate).where(
                        # 同时限定任务 ID 与 candidate 状态，跳过已经处理的条目。
                        MemoryCandidate.job_id == job["id"],
                        # 只取尚未消费的候选，不重复处理已持久化项。
                        MemoryCandidate.status == "candidate",
                    )
                )
            ]

    # 执行可恢复的两阶段流程：提炼证据候选，再判断关系并事务整合。
    async def process(self, job):
        # 先取得当前授权的来源会话快照。
        snapshot = await asyncio.to_thread(self.store.snapshot, job["conversation_id"], job["owner_id"])
        # 任务登记的会话版本和记忆版本必须仍匹配。
        if snapshot["revision"] != job["revision"] or snapshot["memory_version"] != job["memory_version"]:
            # 已出现新回合或设置变化时，让旧任务结束而非继续处理。
            raise AppError("memory_job_stale", "会话已更新", 409)
        # 检查上次执行是否已经保存第一阶段候选。
        pending = await asyncio.to_thread(self.pending, job)
        # 只有尚未保存候选阶段时才重新调用提炼模型。
        if pending is None:
            # Raw approved user statements only; RAG snippets are not tool-verified facts.
            # 自动提炼只使用用户明确原话，不把检索片段或助手推断当已验证事实。
            events = [
                # 保留通过下面条件筛选的完整事件。
                e
                # 遍历快照中的获准历史事件。
                for e in snapshot["events"]
                # 仅接受 user_message 类型。
                if e["kind"] == "user_message"
                # 全部遗忘之后，只考虑遗忘边界以后的陈述。
                and e["created_at"] > snapshot["forget_before"]
                # 预先排除含敏感信息的原始陈述。
                and not sensitive(e["content"]["text"])
            ]
            # 准备按字节成本分批的事件列表。
            batches, batch, size = [], [], 0
            # 依次处理可提炼的用户事件。
            for event in events:
                # 取出用户原始文字。
                text = event["content"]["text"]
                # 以 UTF-8 字节长度估算批次大小。
                cost = len(text.encode())
                # 已有批次追加后超过 24,000 字节时先结束该批；这里不拆开单个事件。
                if batch and size + cost > 24000:
                    # 保存已收集的完整批次。
                    batches.append(batch)
                    # 重置本批事件与累计字节数。
                    batch, size = [], 0
                # 把事件 ID、角色和原文一并交给提炼模型。
                batch.append({"id": event["id"], "role": event["role"], "text": text})
                # 累加本批成本。
                size += cost
            # 循环结束后处理最后的非空批次。
            if batch:
                # 保存尾批事件。
                batches.append(batch)
            # 最多接受 16 个提炼批次，限制单任务调用成本。
            if len(batches) > 16:
                # 超出规模时报告预算限制，不悄悄忽略后半段历史。
                raise AppError("memory_extract_limit", "会话提炼规模超过单任务预算", 413)
            # 收集通过全部筛选的候选。
            candidates = []
            # 建立真实事件 ID 到原文的映射，核验模型引用。
            event_map = {e["id"]: e for e in events}
            # 逐批调用提炼流程。
            for batch in batches:
                # memory_extract 返回符合 Candidate 协议的记忆列表。
                output = await self.llm.call("memory_extract", {"events": batch})
                # 逐条处理本批模型输出。
                for c in output["memories"]:
                    # 只从真实原始事件集合中解析候选引用。
                    source = [event_map[eid] for eid in c["evidence_ids"] if eid in event_map]
                    # 任一证据或内容条件不满足就拒绝候选。
                    if (
                        # 引用无法全部映射到真实事件时拒绝。
                        len(source) != len(c["evidence_ids"])
                        # 当前自动链路只接纳用户明确陈述类型。
                        or c["evidence_type"] != "user_explicit"
                        # 候选任何字段包含敏感信息时拒绝。
                        or sensitive(c)
                        # 逐字引文必须完整出现在至少一条所引用原文中。
                        or not any(c["quote"] in e["content"]["text"] for e in source)
                        # 引文包含猜测、假设或问句标记时不自动升级为事实。
                        or re.search(r"可能|也许|假如|假设|是否|[?？]", c["quote"])
                    ):
                        # 跳过本条不可信候选，继续处理其他输出。
                        continue
                    # 效用低于 0.45 的候选不值得长期保留。
                    if utility(c) < 0.45:
                        # 丢弃低效用候选。
                        continue
                    # 对完整候选执行输出内容审查。
                    await self.moderation.guard(json.dumps(c, ensure_ascii=False), "output")
                    # 通过独立模型调用核验候选是否有原文依据。
                    check = await self.llm.call(
                        # 使用专门的 memory_verify 协议。
                        "memory_verify",
                        {
                            # 提供候选的结构化陈述。
                            "candidate": c,
                            # 只提供候选实际引用的真实事件及原文。
                            "events": [{"id": e["id"], "text": e["content"]["text"]} for e in source],
                        },
                    )
                    # 语义核验判定有依据时才继续准备持久化。
                    if check["supported"]:
                        # Scope is derived from evidence, never from an LLM-supplied tenant/base identifier.
                        # 从服务器记录的证据依赖计算知识库范围，不采信模型生成的库标识。
                        bases = set().union(
                            # 合并被引用事件涉及的所有知识库 ID。
                            *(set(e["content"].get("deps", {}).get("bases", {})) for e in source)
                        )
                        # 来源只涉及一个库时绑定该库；多库时仍由证据依赖保留授权约束。
                        c["base_id"] = next(iter(bases)) if len(bases) == 1 else None
                        # 加入通过全部检查的候选列表。
                        candidates.append(c)
            # 第一阶段结果和任务阶段切换一起落库。
            await asyncio.to_thread(self.stage_candidates, job, candidates)
            # 重新读取持久候选，确保后续以数据库结果为准。
            pending = await asyncio.to_thread(self.pending, job)
        # 准备第二阶段模型关系结果和向量。
        prepared = []
        # 只处理仍待整合的候选。
        for candidate in pending:
            # 取得候选业务字段。
            c = candidate["payload"]
            # 构造主体、属性和值的检索文本。
            text = f"{c['subject']} {c['predicate']} {c['value']}"
            # 在任务所属用户及来源会话范围内召回近邻记忆。
            neighbors = await self.retriever.search(job["owner_id"], text, snapshot["scope"])
            # 没有近邻时默认视为独立事实。
            relation = {"relation": "independent", "neighbor_id": None}
            # 存在可用近邻时才调用关系判定模型。
            if neighbors:
                # 请求模型识别重复、细化、冲突或独立关系。
                relation = await self.llm.call(
                    # 使用专门的 memory_relation 任务。
                    "memory_relation",
                    # 只发送最多四条近邻的 ID 与内容，限制比较成本。
                    {"candidate": c, "neighbors": [{"id": n["id"], **n["payload"]} for n in neighbors[:4]]},
                )
                # 模型给出邻居 ID 时，必须属于服务器实际召回集合。
                if relation["neighbor_id"] is not None and relation["neighbor_id"] not in {
                    # 构造允许被引用的邻居 ID 集合。
                    n["id"]
                    # 允许引用的邻居来自实际授权召回集合。
                    for n in neighbors
                }:
                    # 模型编造或越界引用邻居时拒绝整合。
                    raise AppError("memory_relation_invalid", "记忆整合返回无效来源", 502)
            # 根据配置为候选陈述生成向量，关闭时为 None。
            vector = (await self.embedder.embed([text]))[0] if self.settings.memory_vector_enabled else None
            # 将候选、关系和向量组合为待事务提交的数据。
            prepared.append({**candidate, "relation": relation, "vector": vector})
        # 在线程中执行第二阶段的数据库整合与最终来源校验。
        await asyncio.to_thread(self.consolidate, job, prepared)

    # 将模型判定转换成受事务、版本与删除墓碑约束的记忆变更。
    def consolidate(self, job, prepared):
        # 整批候选整合和任务完成状态在同一事务提交。
        with self.store.db.session.begin() as session:
            # 先检查租约令牌、来源版本及用户设置是否仍有效。
            row, state, conversation = self.fenced(session, job)
            # 依次处理已完成外部模型调用的候选。
            for item in prepared:
                # 刷新候选当前状态，兼容任务重试。
                candidate = session.get(MemoryCandidate, item["id"], populate_existing=True)
                # 已被处理或删除的候选不能重复落库。
                if candidate is None or candidate.status != "candidate":
                    # 跳过已经消费过的候选。
                    continue
                # 读取准备写入的结构化候选。
                c = item["payload"]
                # 提交前再次检查所有证据的当前授权与状态。
                if not self.store.evidence_accessible(
                    # 范围由服务器会话实体给出，不由模型决定。
                    session,
                    # 用任务所属用户验证候选证据。
                    row.owner_id,
                    # 检查候选声明的全部原始事件 ID。
                    c["evidence_ids"],
                    # 按当前会话的真实资料范围重新验证。
                    self.store.scope(conversation),
                ):
                    # 证据失效时将候选标记为 rejected。
                    candidate.status = "rejected"
                    # 继续整合其他候选，不写入失效事实。
                    continue
                # 计算候选默认所属的稳定事实键。
                key = self.key(c.get("base_id"), c["subject"], c["predicate"])
                # 查询该用户同一稳定键下的历史版本。
                existing = session.scalars(
                    # 从记忆记录表读取已有链条。
                    select(MemoryRecord).where(
                        # 同时限制个人归属和稳定键。
                        MemoryRecord.owner_id == row.owner_id,
                        # 只读取当前事实键对应的版本链。
                        MemoryRecord.canonical_key == key,
                    )
                    # 取得整个版本链，便于查墓碑、重复值和冲突。
                ).all()
                # 按关系模型输出读取可能关联的邻居。
                neighbor = (
                    # 读取指定的邻居记录。
                    session.get(MemoryRecord, item["relation"]["neighbor_id"])
                    # 模型确实给出了邻居 ID 才查询。
                    if item["relation"]["neighbor_id"]
                    # 独立关系没有邻居时使用 None。
                    else None
                )
                # 邻居还要通过事务内的基本身份与状态复查。
                if neighbor and (
                    # 不得关联其他用户的记忆。
                    neighbor.owner_id != row.owner_id
                    # 不得跨越候选知识库范围合并稳定键。
                    or neighbor.base_id != c.get("base_id")
                    # 邻居必须仍处于 active，不能基于已删除或争议版本自动整合。
                    or neighbor.status != "active"
                ):
                    # 不满足条件则放弃该邻居关联。
                    neighbor = None
                # 非独立关系且邻居有效时，以邻居事实键作为整合链条。
                if neighbor and item["relation"]["relation"] in {"duplicate", "refinement", "contradiction"}:
                    # 切换到邻居的稳定键。
                    key = neighbor.canonical_key
                    # 重新读取对应链条，后续去重与墓碑检查使用同一身份。
                    existing = session.scalars(
                        # 查询邻居所属事实链的全部版本。
                        select(MemoryRecord).where(
                            # 仍严格限制用户及目标事实键。
                            MemoryRecord.owner_id == row.owner_id,
                            # 切换邻居事实键后，以新键重新查询全部版本。
                            MemoryRecord.canonical_key == key,
                        )
                        # 载入切换后的版本集合。
                    ).all()
                # Explicit deletion is a tombstone; background extraction cannot silently resurrect it.
                # 链条出现删除墓碑时，后台不能悄悄恢复用户已遗忘的事实。
                if any(r.status == "deleted" for r in existing):
                    # 将该候选拒绝。
                    candidate.status = "rejected"
                    # 跳过后续向量和内容写入。
                    continue
                # 先以规范化后的事实值寻找直接重复。
                duplicate = next(
                    (
                        # 返回第一个匹配的 active 版本。
                        r
                        # 遍历同一稳定键的现有版本。
                        for r in existing
                        # 只有有效且值相同的版本才是文本层面的重复。
                        if r.status == "active" and normalized(r.payload["value"]) == normalized(c["value"])
                    ),
                    # 未找到重复时返回 None。
                    None,
                )
                # 模型判定为重复且邻居仍有效时，可按语义重复处理。
                if item["relation"]["relation"] == "duplicate" and neighbor:
                    # 使用该邻居作为合并证据的目标。
                    duplicate = neighbor
                # 重复记忆只合并证据，不再创建新版本。
                if duplicate:
                    # 按原顺序合并并去重证据 ID。
                    duplicate.evidence_ids = list(dict.fromkeys(duplicate.evidence_ids + c["evidence_ids"]))[
                        # 只保留最后 64 个证据引用，限制单条记忆增长。
                        -64:
                    ]
                    # 记录本次证据补充时间。
                    duplicate.updated_at = time.time()
                    # 标记候选已被作为重复条目消费。
                    candidate.status = "duplicate"
                    # 结束本条处理，避免继续插入新记忆。
                    continue
                # 准备新增版本前检查当前用户的有效及争议记录配额。
                count = session.scalar(
                    # 使用数据库计数。
                    select(func.count())
                    # 计数来源为记忆记录表。
                    .select_from(MemoryRecord)
                    # 只统计占用配额的状态与用户范围。
                    .where(
                        # active 与 disputed 都计入配额。
                        MemoryRecord.owner_id == row.owner_id,
                        # 有效和争议记录都会占用记忆配额。
                        MemoryRecord.status.in_(["active", "disputed"]),
                    )
                )
                # 达到用户记忆上限时不继续新增。
                if count >= self.settings.memory_max_records:
                    # 将该候选标记为拒绝。
                    candidate.status = "rejected"
                    # 继续处理剩余候选。
                    continue
                # 同一事实键已有有效或争议版本时，新值进入 disputed，否则进入 active。
                status = "disputed" if any(r.status in {"active", "disputed"} for r in existing) else "active"
                # Ambiguous refinement or contradiction needs a user's explicit resolution, never newest-wins.
                # 存在不确定细化或矛盾时，旧 active 版本也暂停注入。
                if status == "disputed":
                    # 遍历原事实链中的旧版本。
                    for old in existing:
                        # 只修改仍处于 active 的版本。
                        if old.status == "active":
                            # 标记争议，等待用户明确更正，而非自动采用最新值。
                            old.status = "disputed"
                # 重新计算候选效用分数，用于类别和排序。
                score = utility(c)
                # 效用至少 0.75 时保留原类别，否则作为有期限的 episode 保存。
                category = c["category"] if score >= 0.75 else "episode"
                # 将新的记忆版本加入事务。
                session.add(
                    # 创建版本化记忆实体。
                    MemoryRecord(
                        # 新版本使用独立记录 ID。
                        id=new_id(),
                        # 归属任务用户。
                        owner_id=row.owner_id,
                        # 关联由原始证据确定的库范围。
                        base_id=c.get("base_id"),
                        # 保存最终确定的稳定事实键。
                        canonical_key=key,
                        # 保存经过效用规则调整的类别。
                        category=category,
                        # 保存 active 或 disputed 状态。
                        status=status,
                        # 在事实链最大版本上递增，保留历史关系。
                        version=max((r.version for r in existing), default=0) + 1,
                        # 业务负载只保存事实表达和证据类型，不直接保存模型控制字段。
                        payload={k: c[k] for k in ("subject", "predicate", "value", "evidence_type")},
                        # 保留候选核验过的真实事件 ID。
                        evidence_ids=c["evidence_ids"],
                        # 保存第二阶段外部调用生成的向量。
                        embedding=item["vector"],
                        # 绑定当前 Embedding 配置签名。
                        embedding_signature=embedding_signature(self.settings),
                        # 保存效用评分，供检索综合排序使用。
                        utility=score,
                        # 保存模型给出的结构化置信度。
                        confidence=c["confidence"],
                        # 经历型记忆按配置天数计算失效时间。
                        expires_at=time.time() + self.settings.memory_episode_days * 86400
                        # 只有 episode 类别默认启用该有效期。
                        if category == "episode"
                        # 其他类别默认不设置自动到期时间。
                        else None,
                    )
                )
                # 将候选标记为已持久化，防止重试再次插入。
                candidate.status = "persisted"
                # 刷新事务，使后续候选能看到本批刚插入的版本和配额变化。
                session.flush()
            # 全部处理结束后置任务为 completed，并释放租约。
            row.status, row.lease_token, row.lease_until = "completed", None, 0
            # 记录后台整合审计，只记操作摘要，不写候选正文。
            self.store.security.audit(
                # 审计动作标识为 memory.consolidate。
                "memory.consolidate",
                # 以任务 ID 作为审计目标。
                row.id,
                # 只记录候选数量。
                details={"candidates": len(prepared)},
                # 审计与记忆变更处于同一事务。
                session=session,
                # 明确记录任务所属用户作为操作主体。
                actor_id=row.owner_id,
            )

    # 在失败、取消或超时后更新任务状态，并安排有界重试。
    def finish_error(self, job, code):
        # 在事务内读取并修改任务。
        with self.store.db.session.begin() as session:
            # 刷新任务的当前租约令牌。
            row = session.get(MemoryJob, job["id"], populate_existing=True)
            # 任务不存在或已被接管时，旧 worker 不得改写新执行者的状态。
            if not row or row.lease_token != job["token"]:
                # 直接退出，保留当前拥有者的结果。
                return
            # 根据错误性质和已用尝试数选择结束或重试状态。
            row.status = (
                # 来源或租约过期的旧任务进入 obsolete。
                "obsolete"
                # memory_job_stale 表示继续重试旧快照没有意义。
                if code == "memory_job_stale"
                # 耗尽尝试次数的其他失败进入 failed。
                else "failed"
                # 比较已领取次数和配置上限。
                if row.attempts >= self.settings.memory_job_attempts
                # 仍有次数时进入 retry。
                else "retry"
            )
            # 保存截断错误码并清除租约；不保存模型正文或凭据。
            row.error, row.lease_until, row.lease_token = code[:64], 0, None
            # 按指数退避安排下次运行，最大等待 300 秒。
            row.run_after = time.time() + min(300, 2**row.attempts * 5)

    # 领取并执行最多一个后台任务，供 worker 循环或运维调用。
    async def run_once(self):
        # 在线程中完成数据库领取操作。
        job = await asyncio.to_thread(self.claim)
        # 队列中没有可领取任务时无需继续执行。
        if not job:
            # 返回 False 表示本次没有处理任务。
            return False
        # 为后台执行绑定任务用户身份，并保存旧 actor 令牌。
        token = actor.set({"id": job["owner_id"]})
        # 确保任务失败和取消都能执行状态收尾及身份恢复。
        try:
            # 为完整提炼和整合过程建立追踪区间。
            async with self.tracing.span("memory.pipeline"):
                # 整个任务受统一超时约束，不允许模型调用无限占用 worker。
                async with asyncio.timeout(self.settings.memory_job_timeout):
                    # 执行两阶段持久任务流程。
                    await self.process(job)
        # 应用关闭等取消信号需要单独处理。
        except asyncio.CancelledError:
            # 用 shield 尽量完成取消状态写回，防止收尾被同一取消信号打断。
            await asyncio.shield(asyncio.to_thread(self.finish_error, job, "cancelled"))
            # 继续向上抛出取消，保持异步任务的正常终止语义。
            raise
        # 其他异常转成可持久化的错误码。
        except Exception as exc:
            # 把失败状态更新卸载到线程。
            await asyncio.to_thread(
                # 业务异常保存稳定 code，其他异常只保存类型名，不存敏感错误正文。
                self.finish_error,
                # 传入当前领取任务及其 fencing 令牌。
                job,
                # 只持久化稳定错误码或异常类型，避免保存私密异常正文。
                exc.code if isinstance(exc, AppError) else type(exc).__name__,
            )
        # 无论执行结果如何都恢复调用前的身份。
        finally:
            # 避免后台任务用户污染后续 worker 或请求上下文。
            actor.reset(token)
        # 返回 True 表示确实领取并处理过任务，不等同任务一定成功。
        return True

    # 根据开关启动常驻后台扫描任务。
    async def start(self):
        # 以下任一条件满足时都不自动启动 worker。
        if (
            # 全局记忆功能关闭。
            not self.settings.memory_enabled
            # 单独关闭后台 worker。
            or not self.settings.memory_worker_enabled
            # 测试环境不自动启动循环，避免干扰确定性测试。
            or self.settings.environment == "test"
        ):
            # 保留手动运维入口，但不创建后台任务。
            return

        # 定义不断扫描持久队列的异步循环。
        async def loop():
            # 直到应用关闭发出取消信号才退出。
            while True:
                # 隔离单次扫描的故障，避免整个 worker 因一次异常停止。
                try:
                    # 每轮最多处理一个任务。
                    await self.run_once()
                # 正常关闭时需要透传取消异常。
                except asyncio.CancelledError:
                    # 终止循环，不将取消当普通失败重试。
                    raise
                # 扫描或基础设施异常会被观测，但不会输出私密内容。
                except Exception:
                    # Durable rows retain state; expose failed scans without logging credentials/content.
                    # 记录 worker 扫描失败指标，任务状态仍保存在数据库中。
                    self.tracing.monitoring.record("memory.worker", 0, False)
                # 每轮按配置等待，避免空队列时高频轮询。
                await asyncio.sleep(self.settings.memory_worker_interval)

        # 创建后台 Task 并保存句柄，供关闭时取消和等待。
        self.worker = asyncio.create_task(loop())

    # 在应用生命周期结束时停止后台任务。
    async def close(self):
        # 只有已启动 worker 时才需要清理。
        if self.worker:
            # 发送协作取消信号。
            self.worker.cancel()
            # 取消异常属于预期关闭结果，可以抑制。
            with contextlib.suppress(asyncio.CancelledError):
                # 等待任务执行 finally 和持久状态收尾。
                await self.worker

    # 列出当前用户最近的后台记忆任务状态。
    def jobs(self, uid):
        # 通过独立读取会话获取列表。
        with self.store.db.session() as session:
            # 转换成只包含运维状态的字典列表。
            return [
                {
                    # 任务 ID 供前端查询和定位。
                    "id": j.id,
                    # 标明来源会话。
                    "conversation_id": j.conversation_id,
                    # 显示 extract 或 consolidate 阶段。
                    "stage": j.stage,
                    # 显示等待、运行、重试、完成等状态。
                    "status": j.status,
                    # 显示已经领取的尝试次数。
                    "attempts": j.attempts,
                    # 只展示保存的错误码，不输出异常正文。
                    "error": j.error,
                    # 显示下一次允许执行的时间。
                    "run_after": j.run_after,
                }
                # 遍历该用户的任务查询结果。
                for j in session.scalars(
                    # 查询持久任务表。
                    select(MemoryJob)
                    # 强制限制为当前用户。
                    .where(MemoryJob.owner_id == uid)
                    # 按创建时间倒序展示。
                    .order_by(MemoryJob.created_at.desc())
                    # 最多返回最近 50 条任务。
                    .limit(50)
                )
            ]

    # 用户主动发起一次会话记忆提炼，沿用持久任务与幂等规则。
    def enqueue(self, uid, cid):
        # 获取会话租约，避免取版本时与并发问答交叉。
        token = self.store.repository.claim(cid, uid)
        # 保证异常路径也释放租约。
        try:
            # 事件同步和任务创建放在同一事务中。
            with self.store.db.session.begin() as session:
                # 验证会话确实属于当前用户。
                row = self.store.repository._owned(session, cid, uid)
                # 锁定个人记忆状态并读取当前版本。
                state = self.store.state(session, uid, lock=True)
                # 手动提炼也要求开启记忆和自动提炼设置。
                if not state.enabled or not state.auto_extract:
                    # 配置关闭时返回可操作的提示。
                    raise AppError("memory_disabled", "请先开启记忆和自动提炼", 409)
                # 先把已提交消息同步成证据事件。
                self.store.sync_events(session, row)
                # 按会话、上下文版本和个人记忆版本确定任务身份。
                jid = digest([cid, row.context_revision, state.version])[:32]
                # 查找这一状态是否已经有任务。
                job = session.get(MemoryJob, jid)
                # 仅不存在时创建新任务。
                if job is None:
                    # 构造立即可运行的提炼任务。
                    job = MemoryJob(
                        # 使用确定性任务 ID，避免重复点击产生重复任务。
                        id=jid,
                        # 绑定当前用户。
                        owner_id=uid,
                        # 绑定当前会话。
                        conversation_id=cid,
                        # 记录本次来源上下文版本。
                        revision=row.context_revision,
                        # 记录个人记忆状态版本。
                        memory_version=state.version,
                        # 初始状态为 pending。
                        status="pending",
                        # 从 extract 阶段开始。
                        stage="extract",
                        # 用户主动触发不再等待空闲窗口。
                        run_after=time.time(),
                    )
                    # 把任务加入当前事务。
                    session.add(job)
                # 已有等待、失败或重试任务时，允许用户重新安排立即执行。
                elif job.status in {"pending", "failed", "retry"}:
                    # 恢复 pending、取消等待并重置尝试次数；已持久化阶段仍可复用。
                    job.status, job.run_after, job.attempts = "pending", time.time(), 0
                # 返回实际任务 ID 和状态；运行或已完成任务不会重复创建。
                return {"id": jid, "status": job.status}
        # 不论排队结果如何都清理会话租约。
        finally:
            # 只释放本次获得的锁令牌。
            self.store.repository.release(cid, token)

    # 在 Embedding 配置变化后，为当前用户可用记忆重建 SQL 向量字段。
    async def reindex(self, uid):
        # Rebuild only currently accessible active memory vectors after changing embedding space.
        # 只读取当前授权、有效且处于 active 状态的记录。
        rows = await asyncio.to_thread(self.store.list_records, uid)
        # 准备在外部模型调用后批量提交的结果。
        prepared = []
        # 逐条计算需要更新的记忆向量。
        for row in rows:
            # 根据向量开关选择重算或清空向量。
            vector = (
                # 启用时使用统一记忆文本生成向量，关闭时生成 None。
                (await self.embedder.embed([render(row)]))[0] if self.settings.memory_vector_enabled else None
            )
            # 保存 ID 和原版本号，提交时检查模型调用期间是否被更正。
            prepared.append((row["id"], row["version"], vector))

        # 把最终写回封装为同步事务函数。
        def commit():
            # 统计实际成功更新的记录数。
            count = 0
            # 向量写入在数据库事务中完成。
            with self.store.db.session.begin() as session:
                # 锁定用户状态，协调并发纠错和遗忘。
                self.store.state(session, uid, lock=True)
                # 遍历已经完成模型调用的记忆与新向量。
                for mid, version, vector in prepared:
                    # 从数据库刷新当前记录，而不是直接使用调用前的对象。
                    row = session.get(MemoryRecord, mid, populate_existing=True)
                    # 仍可访问且版本未变化时才写回，避免恢复已删除或过期内容。
                    if self.store.record_accessible(session, uid, row) and row.version == version:
                        # 向量与 Embedding 签名一起更新，防止新旧空间混用。
                        row.embedding, row.embedding_signature = vector, embedding_signature(self.settings)
                        # 累加实际重建成功的数量。
                        count += 1
            # 返回事务内成功更新数。
            return count

        # 在线程中提交，并以 reindexed 字段向调用方报告结果。
        return {"reindexed": await asyncio.to_thread(commit)}
