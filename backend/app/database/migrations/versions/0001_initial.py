"""Initial RAG-E schema, frozen independently of application models."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("username", sa.String(64), unique=True, nullable=False),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
    )
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("lock_token", sa.String(32)),
        sa.Column("lock_until", sa.Float, nullable=False),
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "conversation_id",
            sa.String(32),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("details", sa.JSON, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_table(
        "documents",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("chunk_count", sa.Integer, nullable=False),
        sa.Column("error", sa.String(200)),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("ix_documents_status", "documents", ["status"])
    op.create_table(
        "chunks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "document_id", sa.String(32), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("meta", sa.JSON, nullable=False),
        sa.Column("embedding", sa.JSON, nullable=False),
    )
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])


def downgrade():
    for name in ("chunks", "documents", "messages", "conversations", "users"):
        op.drop_table(name)
