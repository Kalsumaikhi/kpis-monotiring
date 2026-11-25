"""Run orchestration: download, parse, diff, guard, apply, notify.

stage_rows, compute_diff and apply_diff share one transaction (staging is a
TEMP table ON COMMIT DROP), so nothing commits between them.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Protocol

import httpx
from sqlalchemy import Engine, case, delete, func, select, text, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.connectors.base import IndicatorMeta
from app.connectors.registry import resolve_connector
from app.core import raw_store, rollup
from app.core.canonical import to_canonical
from app.core.diff import DiffResult, apply_diff, compute_diff, record_pending, stage_rows
from app.models import Change, DataPoint, PendingChange, Run, Source

log = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]


class Notifier(Protocol):
    def notify_run(
        self, run: Run, source: Source, top_indicators: list[tuple[str, int]], streak: int
    ) -> None: ...


class SourceBusyError(Exception):
    """Another run of this source holds the per-source advisory lock."""


class RunNotQueuedError(Exception):
    """The queued run was claimed elsewhere (or is not queued) before we could claim it."""


LOCK_NAMESPACE = 7421  # first key of pg_try_advisory_lock(int, int); second key is source_id


def _acquire_source_lock(session: Session, source_id: int) -> Callable[[], None]:
    """Session-level advisory lock on a dedicated connection; returns the release function.

    If the session is bound to an Engine, a fresh autocommit connection is used so the
    lock outlives the session's own transactions. If it is bound to a Connection (tests),
    that connection is reused.
    """
    bind = session.get_bind()
    own = isinstance(bind, Engine)
    conn = bind.connect().execution_options(isolation_level="AUTOCOMMIT") if own else bind
    params = {"ns": LOCK_NAMESPACE, "sid": source_id}
    try:
        got = conn.execute(text("SELECT pg_try_advisory_lock(:ns, :sid)"), params).scalar()
    except Exception:
        if own:
            conn.close()
        raise
    if not got:
        if own:
            conn.close()
        raise SourceBusyError(f"source {source_id} is busy")

    def release() -> None:
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:ns, :sid)"), params)
        except Exception:  # noqa: BLE001 - a dead connection has dropped the lock anyway
            log.exception("advisory unlock failed for source %s", source_id)
        finally:
            if own:
                conn.close()

    return release


_URL = re.compile(r"https?://\S+")


def _safe_error(e: Exception) -> str:
    """Error text for storage: URLs (which may embed secrets) redacted, truncated."""
    msg = _URL.sub("<url>", f"{type(e).__name__}: {e}")
    return msg[:2000]


def _safe_prune(session: Session, source_id: int) -> None:
    try:
        raw_store.prune(session, source_id, get_settings().raw_retention_runs)
    except Exception:  # noqa: BLE001 - housekeeping must not change the run outcome
        log.exception("raw prune failed for source %s", source_id)
        session.rollback()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_rows(rows: Iterable):
    for r in rows:
        c = to_canonical(r)
        if c is not None:
            yield c


def _upsert_indicators(session: Session, source_id: int, metas: list[IndicatorMeta]) -> None:
    # indicators.first_seen_at defaults to now() on insert and is never updated.
    for m in metas:
        session.execute(
            text(
                """
                INSERT INTO indicators (source_id, code, name, unit, description)
                VALUES (:sid, :code, :name, :unit, :description)
                ON CONFLICT (source_id, code) DO UPDATE
                SET name = EXCLUDED.name, unit = EXCLUDED.unit,
                    description = EXCLUDED.description
                """
            ),
            {"sid": source_id, "code": m.code, "name": m.name, "unit": m.unit,
             "description": m.description},
        )


def _streak(session: Session, run: Run) -> int:
    ids_statuses = session.execute(
        select(Run.status)
        .where(Run.source_id == run.source_id, Run.id <= run.id)
        .order_by(Run.id.desc())
    ).scalars()
    n = 0
    for status in ids_statuses:
        if status != "failed":
            break
        n += 1
    return n


def _top_indicators(session: Session, run: Run) -> list[tuple[str, int]]:
    if run.status != "ok":
        return []
    rows = session.execute(
        select(Change.indicator, func.count().label("n"))
        .where(Change.run_id == run.id)
        .group_by(Change.indicator)
        .order_by(func.count().desc(), Change.indicator)
        .limit(5)
    ).all()
    return [(i, n) for i, n in rows]


def _notify(session: Session, run: Run, notifier) -> None:
    if notifier is None:
        return
    try:
        source = session.get(Source, run.source_id)
        notifier.notify_run(run, source, _top_indicators(session, run), _streak(session, run))
    except Exception as e:  # noqa: BLE001 - notifier problems must not fail the run
        log.error("notifier failed for run %s: %s", run.id, _safe_error(e))
        session.rollback()
        session.execute(
            update(Run).where(Run.id == run.id).values(notify_error=_safe_error(e))
        )
        session.commit()
        session.refresh(run)


def _detach(session: Session, run: Run) -> Run:
    """Load all attributes, then detach so the caller can read the run after close."""
    session.refresh(run)
    session.expunge(run)
    return run


def _fail(session: Session, run_id: int, e: Exception) -> Run:
    session.rollback()
    run = session.get(Run, run_id)
    run.status = "failed"
    run.error = _safe_error(e)
    run.finished_at = _now()
    session.commit()
    return run


HELD_STATUSES = ("suspect_pending", "approve_queued")


def _supersede_held(session: Session, source_id: int, before_id: int | None = None,
                    except_id: int | None = None) -> None:
    """Mark older held runs superseded and drop their pending changes."""
    q = update(Run).where(Run.source_id == source_id, Run.status.in_(HELD_STATUSES))
    if before_id is not None:
        q = q.where(Run.id < before_id)
    if except_id is not None:
        q = q.where(Run.id != except_id)
    # CASE reads the pre-update status, so the message says whether an approval was lost.
    msg = case(
        (Run.status == "approve_queued",
         "superseded by a newer run before the approval was applied"),
        else_="superseded by a newer run",
    )
    ids = session.execute(
        q.values(status="superseded", error=msg).returning(Run.id)
    ).scalars().all()
    if ids:
        session.execute(delete(PendingChange).where(PendingChange.run_id.in_(ids)))


def _finish(session: Session, run: Run, status: str, diff: DiffResult, metas, vintage, warnings=None) -> None:
    run.status = status
    run.rows_total = diff.total
    run.added, run.altered, run.removed = diff.added, diff.altered, diff.removed
    run.vintage = vintage
    if warnings:
        run.error = "; ".join(warnings)[:2000]
    run.finished_at = _now()
    if status != "suspect_pending":
        # Metadata of an unapproved (held) file must not reach the indicators table.
        _upsert_indicators(session, run.source_id, metas)
        if status == "ok":
            rollup.refresh_rollup(session, run.id)  # same transaction as the applied changes
        # Newer good data makes any earlier held run stale: approving it would re-apply old removals.
        _supersede_held(session, run.source_id, before_id=run.id)
    session.commit()
    _safe_prune(session, run.source_id)


def _after_run(session: Session, connector, run: Run) -> None:
    """Optional connector hook for non-failed runs; its failure never fails the run."""
    hook = getattr(connector, "after_run", None)
    if hook is None:
        return
    try:
        hook(session, run)
    except Exception as e:  # noqa: BLE001
        log.error("after_run failed for run %s: %s", run.id, _safe_error(e))
        session.rollback()


def _on_failed(session: Session, connector, run: Run) -> None:
    """Optional connector hook for failed runs that have a raw file; never raises."""
    hook = getattr(connector, "on_failed", None)
    if hook is None:
        return
    try:
        hook(session, run)
    except Exception as e:  # noqa: BLE001
        log.error("on_failed failed for run %s: %s", run.id, _safe_error(e))
        session.rollback()


def run_source(
    session_factory: SessionFactory,
    slug: str,
    trigger: str = "schedule",
    notifier: Notifier | None = None,
    run_id: int | None = None,
    force_hold: bool = False,
) -> Run:
    """Run one source. With run_id, reuse that queued Run row (keeping its trigger).

    force_hold holds any run that has changes as suspect_pending (never a baseline).
    """
    settings = get_settings()
    session = session_factory()
    release = None
    try:
        connector = resolve_connector(session, slug)  # KeyError for unknown slug, before any row
        source = session.scalars(select(Source).where(Source.slug == slug)).one()
        release = _acquire_source_lock(session, source.id)  # SourceBusyError before any row
        if run_id is not None:
            claimed = session.execute(
                update(Run)
                .where(Run.id == run_id, Run.source_id == source.id, Run.status == "queued")
                .values(status="running", started_at=func.now())
            ).rowcount
            session.commit()
            if claimed == 0:
                raise RunNotQueuedError(f"run {run_id} is not a queued run of {slug}")
            run = session.get(Run, run_id)
        else:
            run = Run(source_id=source.id, status="running", trigger=trigger)
            session.add(run)
            session.commit()
        run_id = run.id

        try:
            suffix = connector.raw_suffix
            dest = raw_store.path_for(slug, run_id, suffix)
            run.raw_path = str(dest)
            session.commit()
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                connector.download(client, dest)
        except Exception as e:  # noqa: BLE001
            run = _fail(session, run_id, e)
            _on_failed(session, connector, run)
            _notify(session, run, notifier)
            return _detach(session, run)

        try:
            parsed = connector.parse(dest)
            stage_rows(session, _canonical_rows(parsed.rows))
            diff = compute_diff(session, source.id, parsed.complete)
            has_history = session.scalar(
                select(
                    select(DataPoint.id).where(DataPoint.source_id == source.id).exists()
                    | select(Run.id)
                    .where(Run.source_id == source.id, Run.id < run_id,
                           Run.status.in_(("baseline", "ok")))
                    .exists()
                )
            )
            run = session.get(Run, run_id)
            if not has_history:
                apply_diff(session, source.id, run_id, parsed.complete, record_changes=False)
                status = "baseline"
            elif (
                (diff.existing > 0 and diff.removed / diff.existing > settings.suspect_removal_ratio)
                or (force_hold and diff.added + diff.altered + diff.removed > 0)
            ):
                status = "suspect_pending"
                _supersede_held(session, source.id, except_id=run_id)
                record_pending(session, source.id, run_id, parsed.complete)
            elif diff.added + diff.altered + diff.removed == 0:
                status = "no_change"
            else:
                apply_diff(session, source.id, run_id, parsed.complete, record_changes=True)
                status = "ok"
            _finish(session, run, status, diff, parsed.indicators, parsed.vintage, parsed.warnings)
        except Exception as e:  # noqa: BLE001
            run = _fail(session, run_id, e)
            _on_failed(session, connector, run)
            _notify(session, run, notifier)
            return _detach(session, run)

        _after_run(session, connector, run)
        _notify(session, run, notifier)
        return _detach(session, run)
    finally:
        session.close()
        if release is not None:
            release()


def approve_run(session_factory: SessionFactory, run_id: int, notifier: Notifier | None = None) -> Run:
    session = session_factory()
    release = None
    try:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"run {run_id} not found")
        source_id = run.source_id
        session.rollback()  # end the read transaction; state is re-read under the lock
        release = _acquire_source_lock(session, source_id)  # SourceBusyError if busy
        run = session.get(Run, run_id)
        session.refresh(run)
        latest = session.scalar(
            select(func.max(Run.id)).where(
                Run.source_id == run.source_id, Run.status.in_(HELD_STATUSES)
            )
        )
        if run.status not in HELD_STATUSES or latest != run.id:
            raise ValueError(f"run {run_id} is not the latest held (suspect_pending/approve_queued) run")
        if not run.raw_path:
            raise ValueError(f"run {run_id} has no raw file")
        source = session.get(Source, run.source_id)
        connector = resolve_connector(session, source.slug)
        try:
            parsed = connector.parse(Path(run.raw_path))
            stage_rows(session, _canonical_rows(parsed.rows))
            diff = compute_diff(session, source.id, parsed.complete)
            apply_diff(session, source.id, run.id, parsed.complete, record_changes=True)
            run.trigger = "approve"
            run.error = None
            # The raw file is the single source of truth: changes were just recorded from it.
            session.execute(delete(PendingChange).where(PendingChange.run_id == run.id))
            _finish(session, run, "ok", diff, parsed.indicators, parsed.vintage, parsed.warnings)
        except Exception:
            session.rollback()
            raise
        _notify(session, run, notifier)
        return _detach(session, run)
    finally:
        session.close()
        if release is not None:
            release()


def reject_run(session_factory: SessionFactory, run_id: int) -> Run:
    session = session_factory()
    release = None
    try:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"run {run_id} not found")
        source_id = run.source_id
        session.rollback()
        release = _acquire_source_lock(session, source_id)  # SourceBusyError if busy
        run = session.get(Run, run_id)
        session.refresh(run)
        if run.status != "suspect_pending":
            raise ValueError(f"run {run_id} is not suspect_pending")
        run.status = "rejected"
        run.finished_at = _now()
        session.execute(delete(PendingChange).where(PendingChange.run_id == run.id))
        session.commit()
        return _detach(session, run)
    finally:
        session.close()
        if release is not None:
            release()
