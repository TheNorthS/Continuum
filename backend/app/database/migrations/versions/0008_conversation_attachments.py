"""Persist conversation attachment selection without copying attachment contents."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = depends_on = None


def upgrade():
    # 旧会话没有可靠的浏览器选择快照，默认空列表，不擅自从历史消息推断选择。
    op.add_column(
        "conversations", sa.Column("attachment_ids", sa.JSON(), nullable=False, server_default="[]")
    )


def downgrade():
    op.drop_column("conversations", "attachment_ids")
