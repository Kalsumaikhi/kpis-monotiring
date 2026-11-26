"""Login key kind: login_url/selectors/extra_hosts, login_sessions and login_attempts

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("credentials", sa.Column("login_url", sa.Text()))
    op.add_column("credentials", sa.Column("selectors", JSONB()))
    op.add_column("credentials", sa.Column("extra_hosts", JSONB()))
    op.create_table(
        "login_sessions",
        sa.Column("credential_id", sa.BigInteger(),
                  sa.ForeignKey("credentials.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("state_enc", sa.LargeBinary()),
        sa.Column("status", sa.Text(), server_default="login_needed", nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("credential_id", sa.BigInteger(),
                  sa.ForeignKey("credentials.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("code_enc", sa.LargeBinary()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("waiting_since", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_login_attempts_credential_id", "login_attempts", ["credential_id"])
    op.execute(
        "CREATE UNIQUE INDEX uq_login_attempts_active ON login_attempts (credential_id) "
        "WHERE status IN ('pending', 'signing_in', 'waiting_code', 'verifying')"
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM credentials WHERE kind = 'login' LIMIT 1")).first():
        raise RuntimeError("delete login keys before downgrading 0008")
    op.drop_index("uq_login_attempts_active", table_name="login_attempts")
    op.drop_index("ix_login_attempts_credential_id", table_name="login_attempts")
    op.drop_table("login_attempts")
    op.drop_table("login_sessions")
    op.drop_column("credentials", "extra_hosts")
    op.drop_column("credentials", "selectors")
    op.drop_column("credentials", "login_url")
