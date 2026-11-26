"""sources.needs_baseline_at: when the key-change baseline flag was last set

A key baseline's skip marker left by runs under an earlier key must not count toward the
current key's limit; runs started before this time are ignored.

Revision ID: 0011
Revises: 0010
"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("needs_baseline_at", sa.DateTime(timezone=True),
                                       nullable=True))


def downgrade() -> None:
    op.drop_column("sources", "needs_baseline_at")
