"""indicators.first_seen_at, backfilled from data_points

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "indicators",
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
    )
    op.execute(
        """
        UPDATE indicators i SET first_seen_at = m.first_seen
        FROM (SELECT source_id, indicator, min(first_seen_at) AS first_seen
              FROM data_points GROUP BY source_id, indicator) m
        WHERE m.source_id = i.source_id AND m.indicator = i.code
        """
    )


def downgrade() -> None:
    op.drop_column("indicators", "first_seen_at")
