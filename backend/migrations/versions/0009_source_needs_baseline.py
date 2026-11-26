"""sources.needs_baseline: the next fetched run after a key change is a silent baseline

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("needs_baseline", sa.Boolean(), nullable=False,
                                       server_default=sa.text("false")))


def downgrade() -> None:
    op.drop_column("sources", "needs_baseline")
