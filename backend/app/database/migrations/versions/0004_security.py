"""Accounts, policy, resource ACLs, session proof replay ledger and audit chain."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "roles",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("permissions", sa.JSON, nullable=False),
        sa.Column("builtin", sa.Boolean, nullable=False),
    )
    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("owner_id", sa.String(32), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.execute(
        sa.text("INSERT INTO knowledge_bases (id,name,created_at) VALUES ('legacy','迁移的共享知识库',0)")
    )
    op.create_table(
        "resource_grants",
        sa.Column(
            "base_id",
            sa.String(32),
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("subject_type", sa.String(8), primary_key=True),
        sa.Column("subject_id", sa.String(64), primary_key=True),
        sa.Column("access", sa.String(8), nullable=False),
    )
    op.add_column("users", sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()))
    op.add_column("users", sa.Column("token_version", sa.Integer, nullable=False, server_default="0"))
    op.add_column(
        "users", sa.Column("must_change_password", sa.Boolean, nullable=False, server_default=sa.false())
    )
    # SQLite allows these unbounded ciphertext values without table rebuilding (which would cascade children).
    # PostgreSQL needs JSON/String columns changed to TEXT explicitly.
    if op.get_bind().dialect.name != "sqlite":
        for table, columns in {
            "conversations": ["title"],
            "messages": ["content", "details"],
            "documents": ["filename", "error", "graph_error"],
            "chunks": ["text", "meta", "embedding"],
        }.items():
            for column in columns:
                op.alter_column(table, column, type_=sa.Text(), postgresql_using=f"{column}::text")
    op.add_column("documents", sa.Column("base_id", sa.String(32), nullable=False, server_default="legacy"))
    op.create_index("ix_documents_base_id", "documents", ["base_id"])
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("public_jwk", sa.JSON, nullable=False),
        sa.Column("thumbprint", sa.String(64), nullable=False),
        sa.Column("refresh_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("revoked", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_table(
        "used_refresh_tokens",
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column(
            "session_id", sa.String(32), sa.ForeignKey("auth_sessions.id", ondelete="CASCADE"), nullable=False
        ),
    )
    op.create_table(
        "request_nonces",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("expires_at", sa.Float, nullable=False),
    )
    op.create_index("ix_request_nonces_expires_at", "request_nonces", ["expires_at"])
    op.create_table(
        "security_state",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("value", sa.Text, nullable=False),
    )
    for name in ("audit", "policy", "encryption"):
        op.execute(
            sa.text("INSERT INTO security_state (id,version,value) VALUES (:id,0,'')").bindparams(id=name)
        )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("at", sa.Float, nullable=False),
        sa.Column("actor_id", sa.String(32)),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource", sa.String(200), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("details", sa.JSON, nullable=False),
        sa.Column("previous", sa.String(64), nullable=False),
        sa.Column("signature", sa.String(64), nullable=False),
    )


def downgrade():
    raise RuntimeError(
        "Security migration cannot be downgraded without decrypting and exporting data explicitly"
    )
