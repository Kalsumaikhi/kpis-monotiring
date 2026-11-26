"""Encrypted credentials table and credential_id on watch_targets / sources

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "credentials",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("header_name", sa.Text()),
        sa.Column("prefix", sa.Text()),
        sa.Column("param_name", sa.Text()),
        sa.Column("username", sa.Text()),
        sa.Column("secret_enc", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
    )
    op.execute("CREATE UNIQUE INDEX uq_credentials_name_lower ON credentials (lower(name))")
    for table in ("watch_targets", "sources"):
        op.add_column(table, sa.Column("credential_id", sa.BigInteger()))
        op.create_foreign_key(
            f"fk_{table}_credential_id", table, "credentials",
            ["credential_id"], ["id"], ondelete="RESTRICT",
        )


def downgrade() -> None:
    for table in ("sources", "watch_targets"):
        op.drop_constraint(f"fk_{table}_credential_id", table, type_="foreignkey")
        op.drop_column(table, "credential_id")
    op.drop_table("credentials")
