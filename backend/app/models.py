from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    LargeBinary,
    BigInteger,
    Boolean,
    Date,
    Integer,
    PrimaryKeyConstraint,
    CHAR,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Run statuses are stored as plain text (no DB enum) so later tasks can add values.
RUN_STATUSES = (
    "running", "ok", "no_change", "failed", "suspect_pending",
    "rejected", "superseded", "baseline", "queued", "approve_queued",
)
CHANGE_TYPES = ("added", "altered", "removed")


class Base(DeclarativeBase):
    pass


def _id() -> Mapped[int]:
    return mapped_column(BigInteger, Identity(), primary_key=True)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = _id()
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    area: Mapped[str] = mapped_column(Text)
    access: Mapped[str] = mapped_column(Text)  # 'api' | 'url' | 'manual' | 'watch'
    cron: Mapped[str] = mapped_column(Text, server_default="0 3 * * *")
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = _now()


class Indicator(Base):
    __tablename__ = "indicators"
    __table_args__ = (UniqueConstraint("source_id", "code"),)
    id: Mapped[int] = _id()
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    code: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = _now()


class DataPoint(Base):
    __tablename__ = "data_points"
    __table_args__ = (
        UniqueConstraint("source_id", "key_hash"),
        Index("ix_data_points_source_indicator", "source_id", "indicator"),
    )
    id: Mapped[int] = _id()
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    key_hash: Mapped[str] = mapped_column(CHAR(40))
    dataset: Mapped[str] = mapped_column(Text)
    indicator: Mapped[str] = mapped_column(Text)
    country: Mapped[str] = mapped_column(Text)
    country_iso3: Mapped[str | None] = mapped_column(Text)
    period: Mapped[str] = mapped_column(Text)
    dims: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    value_num: Mapped[Decimal | None] = mapped_column(Numeric)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_hash: Mapped[str] = mapped_column(CHAR(40))
    first_seen_at: Mapped[datetime] = _now()
    last_changed_at: Mapped[datetime] = _now()


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index("uq_runs_one_queued_per_source", "source_id", unique=True,
              postgresql_where=text("status = 'queued'")),
    )
    id: Mapped[int] = _id()
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    status: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    vintage: Mapped[str | None] = mapped_column(Text)
    rows_total: Mapped[int] = mapped_column(BigInteger, server_default="0")
    added: Mapped[int] = mapped_column(BigInteger, server_default="0")
    altered: Mapped[int] = mapped_column(BigInteger, server_default="0")
    removed: Mapped[int] = mapped_column(BigInteger, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    notify_error: Mapped[str | None] = mapped_column(Text)
    raw_path: Mapped[str | None] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(Text, server_default="schedule")  # schedule|manual|approve


class Change(Base):
    __tablename__ = "changes"
    __table_args__ = (
        Index("ix_changes_source_indicator_created", "source_id", "indicator", "created_at"),
        Index("ix_changes_run_id", "run_id"),
        Index("ix_changes_source_created", "source_id", "created_at"),
    )
    id: Mapped[int] = _id()
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    key_hash: Mapped[str] = mapped_column(CHAR(40))
    change_type: Mapped[str] = mapped_column(Text)  # added|altered|removed
    dataset: Mapped[str] = mapped_column(Text)
    indicator: Mapped[str] = mapped_column(Text)
    country: Mapped[str] = mapped_column(Text)
    period: Mapped[str] = mapped_column(Text)
    dims: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    old_num: Mapped[Decimal | None] = mapped_column(Numeric)
    old_text: Mapped[str | None] = mapped_column(Text)
    new_num: Mapped[Decimal | None] = mapped_column(Numeric)
    new_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class Upload(Base):
    __tablename__ = "uploads"
    id: Mapped[int] = _id()
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    who: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"))


class WatchTarget(Base):
    __tablename__ = "watch_targets"
    __table_args__ = (UniqueConstraint("source_id", "url"),)
    id: Mapped[int] = _id()
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    kpi_name: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, server_default="auto")
    selector: Mapped[str | None] = mapped_column(Text)
    use_browser: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    last_status: Mapped[str] = mapped_column(Text, server_default="never")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    needs_baseline: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = _now()


class WatchSnapshot(Base):
    __tablename__ = "watch_snapshots"
    __table_args__ = (
        Index("ix_watch_snapshots_target_id_desc", "target_id", text("id DESC")),
    )
    id: Mapped[int] = _id()
    target_id: Mapped[int] = mapped_column(ForeignKey("watch_targets.id", ondelete="CASCADE"))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(CHAR(64))
    changed: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = _now()


class PendingChange(Base):
    __tablename__ = "pending_changes"
    __table_args__ = (Index("ix_pending_changes_run_id", "run_id"),)
    id: Mapped[int] = _id()
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    key_hash: Mapped[str] = mapped_column(CHAR(40))
    change_type: Mapped[str] = mapped_column(Text)
    dataset: Mapped[str] = mapped_column(Text)
    indicator: Mapped[str] = mapped_column(Text)
    country: Mapped[str] = mapped_column(Text)
    period: Mapped[str] = mapped_column(Text)
    dims: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    old_num: Mapped[Decimal | None] = mapped_column(Numeric)
    old_text: Mapped[str | None] = mapped_column(Text)
    new_num: Mapped[Decimal | None] = mapped_column(Numeric)
    new_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class ChangeRollup(Base):
    __tablename__ = "change_rollup"
    __table_args__ = (PrimaryKeyConstraint("source_id", "indicator", "day"),)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    indicator: Mapped[str] = mapped_column(Text)
    day: Mapped[Any] = mapped_column(Date)
    added: Mapped[int] = mapped_column(Integer, server_default="0")
    altered: Mapped[int] = mapped_column(Integer, server_default="0")
    removed: Mapped[int] = mapped_column(Integer, server_default="0")


class ImportPreview(Base):
    __tablename__ = "import_previews"
    token: Mapped[str] = mapped_column(Text, primary_key=True)
    rows: Mapped[Any] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()


class Credential(Base):
    """A stored API key / licence credential; the secret is Fernet-encrypted (app.credentials)."""
    __tablename__ = "credentials"
    id: Mapped[int] = _id()
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)  # 'header' | 'query' | 'basic'
    header_name: Mapped[str | None] = mapped_column(Text)
    prefix: Mapped[str | None] = mapped_column(Text)
    param_name: Mapped[str | None] = mapped_column(Text)
    username: Mapped[str | None] = mapped_column(Text)
    secret_enc: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    login_url: Mapped[str | None] = mapped_column(Text)
    selectors: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    extra_hosts: Mapped[list[str] | None] = mapped_column(JSONB)


class LoginSession(Base):
    """Saved browser session (Playwright storage_state, Fernet-encrypted) of a login key."""
    __tablename__ = "login_sessions"
    credential_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("credentials.id", ondelete="CASCADE"), primary_key=True)
    state_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    # 'ok' | 'login_needed' | 'signing_in' | 'waiting_code' | 'failed'
    status: Mapped[str] = mapped_column(Text, server_default="login_needed")
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


ACTIVE_ATTEMPT_STATUSES = ("pending", "signing_in", "waiting_code", "verifying")


class LoginAttempt(Base):
    """One sign-in attempt; at most one active per login (partial unique index)."""
    __tablename__ = "login_attempts"
    id: Mapped[int] = _id()
    credential_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("credentials.id", ondelete="CASCADE"))
    # pending|signing_in|waiting_code|verifying|succeeded|failed|expired|cancelled
    status: Mapped[str] = mapped_column(Text)
    code_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    waiting_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


Index("uq_credentials_name_lower", func.lower(Credential.name), unique=True)
Index("ix_login_attempts_credential_id", LoginAttempt.credential_id)
Index(
    "uq_login_attempts_active", LoginAttempt.credential_id, unique=True,
    postgresql_where=LoginAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
)
