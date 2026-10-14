import hashlib
import json
import time

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.common.context import knowledge_scope
from app.common.errors import AppError
from app.models.entities import (
    Chunk,
    Conversation,
    Document,
    IndexProfile,
    KnowledgeBase,
    Message,
    SecurityState,
    User,
    new_id,
)


def as_dict(row):
    value = {column.key: getattr(row, column.key) for column in row.__table__.columns}
    if isinstance(row, Chunk):
        value["meta"] = {**value["meta"], "document_id": row.document_id}
    return value


class Repository:
    def __init__(self, database):
        self.db = database
        self.security = None

    def allowed_documents(self, s, access="read"):
        if self.security is None:
            raise RuntimeError("Authorization service not configured")
        query = select(Document.id).where(Document.base_id.in_(self.security.visible_bases(s, access)))
        scope = knowledge_scope.get()
        return query.where(Document.base_id.in_(scope)) if scope is not None else query

    def _validate_scope(self, s, base_ids):
        if base_ids is None:
            return None
        ids = sorted(set(base_ids))
        if not ids:
            raise AppError("empty_scope", "请至少选择一个知识库", 422)
        if not set(ids).issubset(set(self.security.visible_bases(s, "read"))):
            raise AppError("scope_unavailable", "所选知识库不存在或访问权限已变化，请重新选择知识库", 404)
        return ids

    def validate_scope(self, base_ids):
        with self.db.session() as s:
            return self._validate_scope(s, base_ids)

    def check_document(self, s, document_id, access="read"):
        document = s.scalar(
            select(Document).where(
                Document.id == document_id, Document.id.in_(self.allowed_documents(s, access))
            )
        )
        if not document:
            raise AppError("not_found", "文档不存在或无权限", 404)
        return document

    def user(self, *, username=None, user_id=None):
        with self.db.session() as s:
            row = s.scalar(select(User).where(User.username == username if username else User.id == user_id))
            return as_dict(row) if row else None

    def ensure_index_profile(self, signature):
        with self.db.session.begin() as s:
            profile = s.get(IndexProfile, "knowledge")
            if profile is None:
                s.add(IndexProfile(key="knowledge", signature=signature))
            elif profile.signature != signature:
                if s.scalar(select(func.count()).select_from(Chunk)):
                    raise AppError(
                        "embedding_space_changed",
                        "Embedding、分块或索引配置已变更，请使用新的数据库与 collection 重新导入资料",
                        503,
                    )
                profile.signature = signature

    def create_user(self, username, password_hash, role="user"):
        try:
            with self.db.session.begin() as s:
                row = User(username=username, password_hash=password_hash, role=role)
                s.add(row)
                s.flush()
                return as_dict(row)
        except IntegrityError as exc:
            raise AppError("username_exists", "用户名已存在", 409) from exc

    def conversations(self, user_id):
        with self.db.session() as s:
            rows = s.scalars(
                select(Conversation)
                .where(Conversation.user_id == user_id)
                .order_by(Conversation.created_at.desc())
                .limit(200)
            )
            return [{k: v for k, v in as_dict(r).items() if not k.startswith("lock_")} for r in rows]

    def create_conversation(self, user_id, title, base_ids=None):
        with self.db.session.begin() as s:
            row = Conversation(user_id=user_id, title=title, base_ids=self._validate_scope(s, base_ids))
            s.add(row)
            s.flush()
            return {k: v for k, v in as_dict(row).items() if not k.startswith("lock_")}

    def conversation(self, conversation_id, user_id):
        with self.db.session() as s:
            row = self._owned(s, conversation_id, user_id)
            return {k: v for k, v in as_dict(row).items() if not k.startswith("lock_")}

    def configure_scope(self, conversation_id, user_id, token, base_ids=None):
        """Called while holding the conversation lease, including scope-only edits."""
        with self.db.session.begin() as s:
            row = self._owned(s, conversation_id, user_id)
            if row.lock_token != token:
                raise AppError("conversation_expired", "会话锁已失效，请重试", 409)
            ids = self._validate_scope(s, base_ids if base_ids is not None else row.base_ids)
            if ids != row.base_ids:
                row.base_ids = ids
                row.scope_start_id = (
                    s.scalar(select(func.max(Message.id)).where(Message.conversation_id == conversation_id))
                    or 0
                )
            names = list(
                s.scalars(
                    select(KnowledgeBase.name)
                    .where(KnowledgeBase.id.in_(ids or []))
                    .order_by(KnowledgeBase.id)
                )
            )
            return {"base_ids": ids, "base_names": names, "scope_start_id": row.scope_start_id}

    def change_scope(self, conversation_id, user_id, base_ids):
        token = self.claim(conversation_id, user_id)
        try:
            self.configure_scope(conversation_id, user_id, token, base_ids)
            return self.conversation(conversation_id, user_id)
        finally:
            self.release(conversation_id, token)

    def check_conversation_scope(self, conversation_id, user_id, base_ids=None):
        with self.db.session() as s:
            row = self._owned(s, conversation_id, user_id)
            return self._validate_scope(s, base_ids if base_ids is not None else row.base_ids)

    def _owned(self, s, conversation_id, user_id):
        row = s.scalar(
            select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user_id)
        )
        if not row:
            raise AppError("not_found", "会话不存在", 404)
        return row

    def history(self, conversation_id, user_id):
        with self.db.session() as s:
            self._owned(s, conversation_id, user_id)
            history = [
                as_dict(r)
                for r in s.scalars(
                    select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id)
                )
            ]
            allowed = set(s.scalars(self.allowed_documents(s)))
            for message in history:
                details = message.get("details") or {}
                sources = details.get("sources") or []

                def source_document(source):
                    doc = source.get("metadata", {}).get("document_id")
                    if not doc and not str(source.get("id", "")).startswith("web-"):
                        chunk = s.get(Chunk, source.get("id", ""))
                        return chunk.document_id if chunk else "missing"
                    return doc

                if any(
                    source_document(source) and source_document(source) not in allowed for source in sources
                ):
                    message["content"] = "该回答引用的部分知识库权限已撤销，内容已隐藏。"
                    message["details"] = {}
            return history

    def claim(self, conversation_id, user_id):
        token = new_id()
        with self.db.session.begin() as s:
            self._owned(s, conversation_id, user_id)
            changed = s.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id, Conversation.lock_until < time.time())
                .values(lock_token=token, lock_until=time.time() + 180)
            )
            if not changed.rowcount:
                raise AppError("conversation_busy", "此会话正在生成回答，请稍后重试", 409)
        return token

    def finish(self, conversation_id, token, question, result):
        with self.db.session.begin() as s:
            self._validate_scope(s, knowledge_scope.get())
            allowed = set(s.scalars(self.allowed_documents(s)))
            if knowledge_scope.get() is not None:
                for source in result.get("sources", []):
                    chunk = s.get(Chunk, source.get("id", ""))
                    if chunk is None or chunk.document_id not in allowed:
                        raise AppError(
                            "source_scope_changed", "回答来源不在当前知识库范围内，请重新提问", 403
                        )
            if any(
                source.get("metadata", {}).get("document_id")
                and source["metadata"]["document_id"] not in allowed
                for source in result.get("sources", [])
            ):
                raise AppError("permission_changed", "生成期间资料权限已变化，请重新提问", 403)
            row = s.scalar(
                select(Conversation).where(
                    Conversation.id == conversation_id, Conversation.lock_token == token
                )
            )
            if not row:
                raise AppError("conversation_expired", "会话锁已失效，请重试", 409)
            s.add_all(
                [
                    Message(conversation_id=conversation_id, role="user", content=question),
                    Message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=result["answer"],
                        details=result,
                    ),
                ]
            )
            if row.title == "新对话":
                row.title = question[:80]

    def release(self, conversation_id, token):
        with self.db.session.begin() as s:
            s.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id, Conversation.lock_token == token)
                .values(lock_token=None, lock_until=0)
            )

    def delete_conversation(self, conversation_id, user_id):
        with self.db.session.begin() as s:
            self._owned(s, conversation_id, user_id)
            changed = s.execute(
                delete(Conversation).where(
                    Conversation.id == conversation_id, Conversation.lock_until < time.time()
                )
            )
            if not changed.rowcount:
                raise AppError("conversation_busy", "生成期间不能删除会话", 409)

    def documents(self):
        with self.db.session() as s:
            writable = set(self.security.visible_bases(s, "write"))
            return [
                {**as_dict(r), "can_write": r.base_id in writable}
                for r in s.scalars(
                    select(Document)
                    .where(Document.id.in_(self.allowed_documents(s)))
                    .order_by(Document.created_at.desc())
                )
            ]

    def ready_document_ids(self):
        """ACL/scoped SQL projection: retrieval does not need decrypted document bodies."""
        with self.db.session() as s:
            return list(s.scalars(self.allowed_documents(s).where(Document.status == "ready")))

    def ready_chunk_ids(self):
        """Validate large indexes without loading/decrypting every vector into memory."""
        with self.db.session() as s:
            return list(
                s.scalars(
                    select(Chunk.id)
                    .join(Document)
                    .where(Document.status == "ready", Document.id.in_(self.allowed_documents(s)))
                )
            )

    def document_by_digest(self, digest, base_id="legacy"):
        with self.db.session() as s:
            self.security.require_base(s, base_id, "write")
            row = s.scalar(select(Document).where(Document.digest == digest))
            return as_dict(row) if row else None

    def reserve_document(self, filename, digest, base_id="legacy"):
        try:
            with self.db.session.begin() as s:
                self.security.require_base(s, base_id, "write")
                row = Document(filename=filename, digest=digest, base_id=base_id)
                s.add(row)
                s.flush()
                return as_dict(row), True
        except IntegrityError:
            with self.db.session() as s:
                row = s.scalar(select(Document).where(Document.digest == digest))
                return as_dict(row), False

    def commit_document(self, document_id, chunks):
        with self.db.session.begin() as s:
            self.check_document(s, document_id, "write")
            s.add_all([Chunk(**c) for c in chunks])
            s.execute(
                update(Document)
                .where(Document.id == document_id)
                .values(status="ready", chunk_count=len(chunks), error=None)
            )
            self.security.audit("document.commit", document_id, details={"chunks": len(chunks)}, session=s)

    def fail_document(self, document_id):
        with self.db.session.begin() as s:
            s.execute(
                update(Document)
                .where(Document.id == document_id)
                .values(status="failed", error="索引失败，请删除后重新上传")
            )

    def mark_deleting(self, document_id):
        with self.db.session.begin() as s:
            row = self.check_document(s, document_id, "write")
            if not row:
                raise AppError("not_found", "文档不存在", 404)
            if row.status == "indexing" and row.created_at > time.time() - 600:
                raise AppError("document_busy", "文档正在索引", 409)
            row.status = "deleting"

    def delete_document(self, document_id):
        with self.db.session.begin() as s:
            self.check_document(s, document_id, "write")
            s.execute(delete(Document).where(Document.id == document_id))
            self.security.audit("document.delete", document_id, session=s)

    def ready_chunks(self):
        with self.db.session() as s:
            return [
                as_dict(r)
                for r in s.scalars(
                    select(Chunk)
                    .join(Document)
                    .where(Document.status == "ready", Document.id.in_(self.allowed_documents(s)))
                )
            ]

    def document_chunks(self, document_id, access="read"):
        with self.db.session() as s:
            document = self.check_document(s, document_id, access)
            if not document or document.status != "ready":
                raise AppError("not_found", "文档不存在或尚未就绪", 404)
            return [
                as_dict(c)
                for c in s.scalars(select(Chunk).where(Chunk.document_id == document_id).order_by(Chunk.id))
            ]

    def graph_status(self, document_id, status, error=None):
        with self.db.session.begin() as s:
            s.execute(
                update(Document)
                .where(Document.id == document_id)
                .values(graph_status=status, graph_error=error)
            )
            self.security.audit("graph." + status, document_id, session=s)

    def chunks_by_ids(self, ids):
        with self.db.session() as s:
            return [
                as_dict(c)
                for c in s.scalars(
                    select(Chunk)
                    .join(Document)
                    .where(
                        Chunk.id.in_(ids),
                        Document.status == "ready",
                        Document.id.in_(self.allowed_documents(s)),
                    )
                )
            ]

    def filter_ready(self, ids):
        if not ids:
            return set()
        with self.db.session() as s:
            # Anchor to the small candidate set. A join can make SQLite visit
            # every ready document and read large encrypted chunk pages first.
            # The correlated existence check retains exactly the same live ACL,
            # selected scope and ready-state predicate in one SQL statement.
            accessible_ready_document = (
                self.allowed_documents(s)
                .where(Document.id == Chunk.document_id, Document.status == "ready")
                .correlate(Chunk)
                .exists()
            )
            return set(s.scalars(select(Chunk.id).where(Chunk.id.in_(ids), accessible_ready_document)))

    def revision(self):
        with self.db.session() as s:
            if self.security is None:
                raise RuntimeError("Authorization service not configured")
            namespace = s.scalar(select(IndexProfile.signature).where(IndexProfile.key == "retrieval_cache"))
            if not namespace or not namespace.get("namespace"):
                raise RuntimeError("Retrieval revision is not initialized; apply database migrations")
            acl_revision = s.scalar(select(SecurityState.version).where(SecurityState.id == "retrieval_acl"))
            if acl_revision is None:
                raise RuntimeError("Retrieval ACL revision is not initialized; apply database migrations")
            visible = set(self.security.visible_bases(s))
            scope = knowledge_scope.get()
            if scope is not None:
                visible.intersection_update(scope)
            # Read O(visible bases) counters, never all document IDs or encrypted chunks.
            versions = s.execute(
                select(KnowledgeBase.id, KnowledgeBase.retrieval_revision)
                .where(KnowledgeBase.id.in_(sorted(visible)))
                .order_by(KnowledgeBase.id)
            )
            payload = [namespace["namespace"], acl_revision, [list(row) for row in versions]]
            return "r3:" + hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()

    def stats(self):
        with self.db.session() as s:
            return {
                name: s.scalar(select(func.count()).select_from(model))
                for name, model in [
                    ("users", User),
                    ("conversations", Conversation),
                    ("documents", Document),
                    ("chunks", Chunk),
                ]
            }
