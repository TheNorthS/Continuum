"""Remember the embedding space so model changes cannot silently corrupt retrieval."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "index_profiles",
        sa.Column("key", sa.String(32), primary_key=True),
        sa.Column("signature", sa.JSON, nullable=False),
    )


def downgrade():
    op.drop_table("index_profiles")
