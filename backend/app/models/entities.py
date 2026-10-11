import time
import uuid

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.common.encryption import Encrypted


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class IndexProfile(Base):
    __tablename__ = "index_profiles"
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    signature: Mapped[dict] = mapped_column(JSON)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="user")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(Encrypted("conversations.title"), default="新对话")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    lock_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lock_until: Mapped[float] = mapped_column(Float, default=0)
    base_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    scope_start_id: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Encrypted("messages.content"))
    details: Mapped[dict] = mapped_column(Encrypted("messages.details"), default=dict)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    filename: Mapped[str] = mapped_column(Encrypted("documents.filename"))
    base_id: Mapped[str] = mapped_column(String(32), default="legacy", server_default="legacy", index=True)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="indexing", index=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Encrypted("documents.error"), nullable=True)
    graph_status: Mapped[str] = mapped_column(String(16), default="disabled", server_default="disabled")
    graph_error: Mapped[str | None] = mapped_column(Encrypted("documents.graph_error"), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Encrypted("chunks.text"))
    meta: Mapped[dict] = mapped_column(Encrypted("chunks.meta"))
    embedding: Mapped[list] = mapped_column(Encrypted("chunks.embedding"))


class Role(Base):
    __tablename__ = "roles"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    permissions: Mapped[list] = mapped_column(JSON, default=list)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(Encrypted("knowledge_bases.name"))
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Grant(Base):
    __tablename__ = "resource_grants"
    base_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), primary_key=True
    )
    subject_type: Mapped[str] = mapped_column(String(8), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    access: Mapped[str] = mapped_column(String(8))


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    public_jwk: Mapped[dict] = mapped_column(JSON)
    thumbprint: Mapped[str] = mapped_column(String(64))
    refresh_hash: Mapped[str] = mapped_column(String(64), unique=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    expires_at: Mapped[float] = mapped_column(Float)


class UsedRefresh(Base):
    __tablename__ = "used_refresh_tokens"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("auth_sessions.id", ondelete="CASCADE"))


class Nonce(Base):
    __tablename__ = "request_nonces"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    expires_at: Mapped[float] = mapped_column(Float, index=True)


class SecurityState(Base):
    __tablename__ = "security_state"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    value: Mapped[str] = mapped_column(Text, default="")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[float] = mapped_column(Float)
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str] = mapped_column(String(100))
    resource: Mapped[str] = mapped_column(String(200))
    outcome: Mapped[str] = mapped_column(String(16))
    request_id: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON)
    previous: Mapped[str] = mapped_column(String(64))
    signature: Mapped[str] = mapped_column(String(64))
