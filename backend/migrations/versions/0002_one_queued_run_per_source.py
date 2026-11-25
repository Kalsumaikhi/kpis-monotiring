"""one queued run per source

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_runs_one_queued_per_source", "runs", ["source_id"], unique=True,
        postgresql_where="status = 'queued'",
    )


def downgrade() -> None:
    op.drop_index("uq_runs_one_queued_per_source", table_name="runs")
