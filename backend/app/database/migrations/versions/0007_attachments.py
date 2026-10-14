"""Private encrypted attachment space and explicit knowledge-free conversations."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "conversations", sa.Column("use_knowledge", sa.Boolean(), nullable=False, server_default="1")
    )
    op.create_table(
        "attachments",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("storage_size", sa.Integer(), nullable=False),
        sa.Column("text_chars", sa.Integer(), nullable=False),
        sa.Column("image_count", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_attachments_owner_id", "attachments", ["owner_id"])


def downgrade():
    op.drop_table("attachments")
    op.drop_column("conversations", "use_knowledge")
