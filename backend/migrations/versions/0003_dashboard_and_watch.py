"""dashboard and watch: watch targets, snapshots, pending changes, rollup, import previews

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _now():
    return sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                     nullable=False)


def upgrade() -> None:
    op.create_table(
        "watch_targets",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("kpi_name", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), server_default="auto", nullable=False),
        sa.Column("selector", sa.Text(), nullable=True),
        sa.Column("use_browser", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_status", sa.Text(), server_default="never", nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        _now(),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "url"),
    )
    op.create_table(
        "watch_snapshots",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("changed", sa.Boolean(), nullable=False),
        _now(),
        sa.ForeignKeyConstraint(["target_id"], ["watch_targets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_watch_snapshots_target_id_desc", "watch_snapshots",
                    ["target_id", sa.text("id DESC")])
    op.create_table(
        "pending_changes",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("key_hash", sa.CHAR(40), nullable=False),
        sa.Column("change_type", sa.Text(), nullable=False),
        sa.Column("dataset", sa.Text(), nullable=False),
        sa.Column("indicator", sa.Text(), nullable=False),
        sa.Column("country", sa.Text(), nullable=False),
        sa.Column("period", sa.Text(), nullable=False),
        sa.Column("dims", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("old_num", sa.Numeric(), nullable=True),
        sa.Column("old_text", sa.Text(), nullable=True),
        sa.Column("new_num", sa.Numeric(), nullable=True),
        sa.Column("new_text", sa.Text(), nullable=True),
        _now(),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pending_changes_run_id", "pending_changes", ["run_id"])
    op.create_table(
        "change_rollup",
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("indicator", sa.Text(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("added", sa.Integer(), server_default="0", nullable=False),
        sa.Column("altered", sa.Integer(), server_default="0", nullable=False),
        sa.Column("removed", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("source_id", "indicator", "day"),
    )
    op.create_table(
        "import_previews",
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("rows", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        _now(),
        sa.PrimaryKeyConstraint("token"),
    )
    op.create_index("ix_changes_source_created", "changes", ["source_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_changes_source_created", table_name="changes")
    op.drop_table("import_previews")
    op.drop_table("change_rollup")
    op.drop_index("ix_pending_changes_run_id", table_name="pending_changes")
    op.drop_table("pending_changes")
    op.drop_index("ix_watch_snapshots_target_id_desc", table_name="watch_snapshots")
    op.drop_table("watch_snapshots")
    op.drop_table("watch_targets")
