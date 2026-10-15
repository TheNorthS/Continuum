"""Durable event projection, fenced compaction and private evidence-grounded memory."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "conversations", sa.Column("context_revision", sa.BigInteger(), nullable=False, server_default="0")
    )
    op.add_column("conversations", sa.Column("context_pins", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("conversations", sa.Column("active_checkpoint_id", sa.String(32), nullable=True))
    op.create_table(
        "memory_states",
        sa.Column("id", sa.String(32), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("auto_extract", sa.Boolean(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("forget_before", sa.Float(), nullable=False),
    )

    def owner():
        return sa.Column("owner_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False)

    def conv(nullable=False):
        return sa.Column(
            "conversation_id",
            sa.String(32),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=nullable,
        )

    op.create_table(
        "memory_events",
        sa.Column("id", sa.String(32), primary_key=True),
        owner(),
        conv(True),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("tool_call_id", sa.String(32), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("conversation_id", "sequence", name="uq_memory_event_sequence"),
    )
    op.create_table(
        "memory_artifacts",
        sa.Column("id", sa.String(32), primary_key=True),
        owner(),
        conv(),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "compaction_checkpoints",
        sa.Column("id", sa.String(32), primary_key=True),
        owner(),
        conv(),
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("start_seq", sa.Integer(), nullable=False),
        sa.Column("end_seq", sa.Integer(), nullable=False),
        sa.Column("base_revision", sa.BigInteger(), nullable=False),
        sa.Column("source_event_ids", sa.JSON(), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("parent_id", sa.String(32), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("tokens_before", sa.Integer(), nullable=False),
        sa.Column("tokens_after", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "memory_records",
        sa.Column("id", sa.String(32), primary_key=True),
        owner(),
        sa.Column("base_id", sa.String(32), nullable=True),
        sa.Column("canonical_key", sa.String(64), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("embedding_signature", sa.String(64), nullable=False),
        sa.Column("utility", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("last_used_at", sa.Float(), nullable=True),
        sa.Column("expires_at", sa.Float(), nullable=True),
        sa.UniqueConstraint("owner_id", "canonical_key", "version", name="uq_memory_version"),
    )
    op.create_table(
        "memory_jobs",
        sa.Column("id", sa.String(32), primary_key=True),
        owner(),
        conv(),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("memory_version", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("run_after", sa.Float(), nullable=False),
        sa.Column("lease_token", sa.String(32), nullable=True),
        sa.Column("lease_until", sa.Float(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "memory_candidates",
        sa.Column("id", sa.String(64), primary_key=True),
        owner(),
        sa.Column(
            "job_id", sa.String(32), sa.ForeignKey("memory_jobs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    for table in (
        "memory_events",
        "memory_artifacts",
        "compaction_checkpoints",
        "memory_records",
        "memory_jobs",
        "memory_candidates",
    ):
        op.create_index(f"ix_{table}_owner_id", table, ["owner_id"])
    for table in ("memory_events", "memory_artifacts", "compaction_checkpoints", "memory_jobs"):
        op.create_index(f"ix_{table}_conversation_id", table, ["conversation_id"])
    for table, column in (
        ("memory_records", "canonical_key"),
        ("memory_records", "status"),
        ("memory_jobs", "status"),
        ("memory_candidates", "job_id"),
    ):
        op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    for table in (
        "memory_candidates",
        "memory_jobs",
        "memory_records",
        "compaction_checkpoints",
        "memory_artifacts",
        "memory_events",
        "memory_states",
    ):
        op.drop_table(table)
    for column in ("active_checkpoint_id", "context_pins", "context_revision"):
        op.drop_column("conversations", column)
