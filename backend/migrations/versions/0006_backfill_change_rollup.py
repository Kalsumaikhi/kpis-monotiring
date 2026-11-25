"""Backfill change_rollup from changes recorded before the rollup existed

Same statement as `python -m app.cli backfill-rollup` (app.core.rollup.backfill), copied here
so the migration does not depend on later app code. Idempotent: the table is rebuilt.

Revision ID: 0006
Revises: 0005
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM change_rollup")
    op.execute(
        """
        INSERT INTO change_rollup (source_id, indicator, day, added, altered, removed)
        SELECT source_id, indicator, (created_at AT TIME ZONE 'UTC')::date AS day,
               count(*) FILTER (WHERE change_type = 'added'),
               count(*) FILTER (WHERE change_type = 'altered'),
               count(*) FILTER (WHERE change_type = 'removed')
        FROM changes
        GROUP BY source_id, indicator, (created_at AT TIME ZONE 'UTC')::date
        """
    )


def downgrade() -> None:
    pass  # data-only migration: the rollup rows stay valid
