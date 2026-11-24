from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.models import Change, DataPoint, Run, Source

router = APIRouter()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


class SourcePatch(BaseModel):
    enabled: bool | None = None
    cron: str | None = None


def _source_or_404(session: Session, slug: str) -> Source:
    source = session.scalars(select(Source).where(Source.slug == slug)).one_or_none()
    if source is None:
        raise HTTPException(404, "source not found")
    return source


def _serialise(source: Source, last_run: Run | None, last_change_at, rows_tracked, changes_30d, streak):
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
            "finished_at": _iso(last_run.finished_at),
            "added": last_run.added,
            "altered": last_run.altered,
            "removed": last_run.removed,
        },
        "last_change_at": _iso(last_change_at),
        "rows_tracked": rows_tracked,
        "changes_30d": changes_30d,
        "failing_streak": streak,
    }


@router.get("/sources")
def list_sources(session: Session = Depends(get_session)):
    sources = session.scalars(select(Source).order_by(Source.slug)).all()
    since = datetime.now(timezone.utc) - timedelta(days=30)

    last_runs = {
        r.source_id: r
        for r in session.scalars(
            select(Run).ext(distinct_on(Run.source_id)).order_by(Run.source_id, Run.id.desc())
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
    return [
        _serialise(
            s, last_runs.get(s.id), change_stats.get(s.id, (None, 0))[0],
            rows.get(s.id, 0), change_stats.get(s.id, (None, 0))[1], streaks.get(s.id, 0),
        )
        for s in sources
    ]


@router.patch("/sources/{slug}")
def patch_source(slug: str, body: SourcePatch, session: Session = Depends(get_session)):
    source = _source_or_404(session, slug)
    if body.cron is not None:
        try:
            CronTrigger.from_crontab(body.cron)
        except Exception:  # noqa: BLE001 - APScheduler raises ValueError or KeyError variants
            raise HTTPException(422, "invalid cron expression") from None
        source.cron = body.cron
    if body.enabled is not None:
        source.enabled = body.enabled
    session.commit()
    return {"slug": source.slug, "enabled": source.enabled, "cron": source.cron}


@router.post("/sources/{slug}/run", status_code=202)
def run_now(slug: str, session: Session = Depends(get_session)):
    source = _source_or_404(session, slug)
    busy = session.scalar(
        select(
            select(Run.id)
            .where(Run.source_id == source.id, Run.status.in_(("queued", "running")))
            .exists()
        )
    )
    if busy:
        raise HTTPException(409, "source already has a queued or running run")
    run = Run(source_id=source.id, status="queued", trigger="manual")
    session.add(run)
    session.commit()
    return {"run_id": run.id}
