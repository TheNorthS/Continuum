"""Persist selected knowledge bases and the context boundary without rebuilding SQLite tables."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column("base_ids", sa.JSON, nullable=True))
    op.add_column(
        "conversations", sa.Column("scope_start_id", sa.Integer, nullable=False, server_default="0")
    )


def downgrade():
    op.drop_column("conversations", "scope_start_id")
    op.drop_column("conversations", "base_ids")
