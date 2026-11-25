"""watch_targets.needs_baseline: new or re-enabled KPIs get a silent first fetch

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "watch_targets",
        sa.Column("needs_baseline", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("watch_targets", "needs_baseline")
