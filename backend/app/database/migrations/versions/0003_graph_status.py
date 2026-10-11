"""Independent graph indexing state on each document."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "documents", sa.Column("graph_status", sa.String(16), nullable=False, server_default="disabled")
    )
    op.add_column("documents", sa.Column("graph_error", sa.String(200), nullable=True))


def downgrade():
    op.drop_column("documents", "graph_error")
    op.drop_column("documents", "graph_status")
