from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.util import iso, source_lock
from app.api.deps import get_session
from app.api.routes_kpis import attribution_for
from app.connectors.registry import resolve_connector
from app.core.pipeline import _supersede_held
from app.models import Change, DataPoint, Credential, Run, Source, WatchTarget

router = APIRouter()


class SourcePatch(BaseModel):
    enabled: bool | None = None
    cron: str | None = None
    credential_id: int | None = None


def _source_or_404(session: Session, slug: str) -> Source:
    source = session.scalars(select(Source).where(Source.slug == slug)).one_or_none()
    if source is None:
        raise HTTPException(404, "source not found")
    return source


def _serialise(source: Source, last_run: Run | None, last_change_at, rows_tracked, changes_30d, streak,
               incomplete=False, noisy_targets=0, last_success_at=None,
               credential_name=None, credential_supported=False, logins_needed=0):
    return {
        "slug": source.slug,
        "name": source.name,
        "area": source.area,
        "access": source.access,
        "enabled": source.enabled,
        "cron": source.cron,
        "last_run": None if last_run is None else {
            "id": last_run.id,
            "status": last_run.status,
            "finished_at": iso(last_run.finished_at),
            "added": last_run.added,
            "altered": last_run.altered,
            "removed": last_run.removed,
        },
        "last_change_at": iso(last_change_at),
        "rows_tracked": rows_tracked,
        "changes_30d": changes_30d,
        "failing_streak": streak,
        "attribution": attribution_for(source.slug),
        "incomplete": incomplete,
        "noisy_targets": noisy_targets,
        "last_success_at": iso(last_success_at),
        "credential_id": source.credential_id,
        "credential_name": credential_name,
        "credential_supported": credential_supported,
        "logins_needed": logins_needed,
    }


def _supports_keys(session: Session, source: Source) -> bool:
    if source.access == "watch":  # custom KPIs carry their keys on the targets
        return False
    try:
        return bool(getattr(resolve_connector(session, source.slug), "credential_hosts", ()))
    except KeyError:
        return False


def _noisy_counts(session: Session) -> dict[int, int]:
    """Number of noisy watch targets (changed in >8 of last 10 snapshots), by source id."""
    from app.connectors.watch import noisy_counts

    return noisy_counts(session)


def _incomplete_ids(session: Session) -> set[int]:
    """Sources whose latest finished run (not failed) has an 'incomplete:' error."""
    return set(
        session.execute(
            text(
                """
                SELECT source_id FROM (
                    SELECT DISTINCT ON (source_id) source_id, status, error FROM runs
                    WHERE status NOT IN ('queued', 'running', 'approve_queued')
                    ORDER BY source_id, id DESC
                ) latest
                WHERE status <> 'failed' AND error LIKE 'incomplete:%'
                """
            )
        ).scalars()
    )


@router.get("/sources")
def list_sources(access: str | None = None, session: Session = Depends(get_session)):
    q = select(Source).order_by(Source.slug)
    if access:
        q = q.where(Source.access == access)
    sources = session.scalars(q).all()
    since = datetime.now(timezone.utc) - timedelta(days=30)

    last_runs = {
        r.source_id: r
        for r in session.scalars(
            select(Run).ext(distinct_on(Run.source_id)).order_by(Run.source_id, Run.id.desc())
        )
    }
    last_success = {
        r.source_id: r.finished_at
        for r in session.scalars(
            select(Run)
            .ext(distinct_on(Run.source_id))
            .where(Run.status.in_(("ok", "no_change", "baseline")), Run.finished_at.is_not(None))
            .order_by(Run.source_id, Run.finished_at.desc())
        )
    }
    change_stats = {
        sid: (last, n30)
        for sid, last, n30 in session.execute(
            select(
                Change.source_id,
                func.max(Change.created_at),
                func.count().filter(Change.created_at >= since),
            ).group_by(Change.source_id)
        )
    }
    rows = dict(
        session.execute(
            select(DataPoint.source_id, func.count()).group_by(DataPoint.source_id)
        ).all()
    )
    streaks = dict(
        session.execute(
            text(
                """
                SELECT r.source_id, count(*) FROM runs r
                WHERE r.status = 'failed' AND r.id > COALESCE(
                    (SELECT max(x.id) FROM runs x
                     WHERE x.source_id = r.source_id AND x.status <> 'failed'), 0)
                GROUP BY r.source_id
                """
            )
        ).all()
    )
    incomplete = _incomplete_ids(session)
    noisy = _noisy_counts(session)
    cred_names = dict(session.execute(select(Credential.id, Credential.name)).all())
    logins_needed = dict(session.execute(
        select(WatchTarget.source_id, func.count()).where(
            WatchTarget.enabled.is_(True), WatchTarget.last_status == "login_needed")
        .group_by(WatchTarget.source_id)).all())
    return [
        _serialise(
            s, last_runs.get(s.id), change_stats.get(s.id, (None, 0))[0],
            rows.get(s.id, 0), change_stats.get(s.id, (None, 0))[1], streaks.get(s.id, 0),
            s.id in incomplete, noisy.get(s.id, 0), last_success.get(s.id),
            cred_names.get(s.credential_id), _supports_keys(session, s), logins_needed.get(s.id, 0),
        )
        for s in sources
    ]


KEY_CHANGE = "a key change"


@router.patch("/sources/{slug}")
def patch_source(slug: str, body: SourcePatch, session: Session = Depends(get_session)):
    source = _source_or_404(session, slug)
    if body.cron is not None:
        try:
            CronTrigger.from_crontab(body.cron)
        except Exception:  # noqa: BLE001 - APScheduler raises ValueError or KeyError variants
            raise HTTPException(422, "invalid cron expression") from None
    rekeys = False
    if "credential_id" in body.model_fields_set:
        if body.credential_id is not None:
            if source.access == "watch":
                raise HTTPException(422, "this source doesn't use keys")
            key = session.get(Credential, body.credential_id)
            if key is None:
                raise HTTPException(422, "unknown key")
            if key.kind == "login":  # a login is a browser session, never a request header
                raise HTTPException(422, "a login can only be used by KPIs")
        rekeys = body.credential_id != source.credential_id
    if not (rekeys and _supports_keys(session, source)):  # a key it never sends changes nothing
        _apply_source_patch(source, body)
        if rekeys:
            source.credential_id = body.credential_id
        session.commit()
    else:
        # With another key (or none) the source may return other data (a key can unlock
        # more), so the next fetched run is a silent baseline and a held run diffed with the
        # old key is dropped. Under the per-source lock, which run_source and approve_run
        # hold for the whole run: no run can be fetching with the old key and then clear
        # the flag, and no approval can apply old-key data. 409 while a run holds it.
        sid = source.id
        with source_lock(session, sid):
            session.refresh(source)  # state may have changed while waiting
            _apply_source_patch(source, body)
            if body.credential_id != source.credential_id:
                source.credential_id = body.credential_id
                source.needs_baseline = True
                source.needs_baseline_at = func.clock_timestamp()
                _supersede_held(session, sid, reason=KEY_CHANGE)
            session.commit()
    return {"slug": source.slug, "enabled": source.enabled, "cron": source.cron,
            "credential_id": source.credential_id}


def _apply_source_patch(source: Source, body: SourcePatch) -> None:
    """The cron and enabled edits; the caller sets the key and commits."""
    if body.cron is not None:
        source.cron = body.cron
    if body.enabled is not None:
        source.enabled = body.enabled


def _has_open_run(session: Session, source_id: int) -> bool:
    return bool(session.scalar(
        select(
            select(Run.id)
            .where(Run.source_id == source_id, Run.status.in_(("queued", "running")))
            .exists()
        )
    ))


@router.post("/sources/{slug}/run", status_code=202)
def run_now(slug: str, session: Session = Depends(get_session)):
    source = _source_or_404(session, slug)
    busy = _has_open_run(session, source.id)
    if busy:
        raise HTTPException(409, "source already has a queued or running run")
    run = Run(source_id=source.id, status="queued", trigger="manual")
    session.add(run)
    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        if "uq_runs_one_queued_per_source" not in str(e.orig):
            raise
        # lost the race against a concurrent run-now
        raise HTTPException(409, "source already has a queued or running run") from None
    return {"run_id": run.id}
