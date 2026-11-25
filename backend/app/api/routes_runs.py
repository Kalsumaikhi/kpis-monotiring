from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.util import iso
from app.api.deps import get_session, get_session_factory
from app.core import pipeline
from app.core.pipeline import SourceBusyError, approve_run, reject_run
from app.models import Change, Run, Source
from app.notify.mattermost import notifier_from_settings

router = APIRouter()


def _json_value(value):
    """Decimals go out as strings so JSON floats never round them."""
    return str(value) if isinstance(value, Decimal) else value


def _run_dict(run: Run, slug: str) -> dict:
    return {
        "id": run.id,
        "source": slug,
        "status": run.status,
        "trigger": run.trigger,
        "started_at": iso(run.started_at),
        "finished_at": iso(run.finished_at),
        "vintage": run.vintage,
        "rows_total": run.rows_total,
        "added": run.added,
        "altered": run.altered,
        "removed": run.removed,
        "error": run.error,
        "notify_error": run.notify_error,
    }


def _run_or_404(session: Session, run_id: int) -> tuple[Run, str]:
    row = session.execute(
        select(Run, Source.slug).join(Source, Source.id == Run.source_id).where(Run.id == run_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(404, "run not found")
    return row[0], row[1]


@router.get("/runs")
def list_runs(
    source: str | None = None,
    status: str | None = None,
    since: datetime | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
):
    q = select(Run, Source.slug).join(Source, Source.id == Run.source_id)
    if source:
        q = q.where(Source.slug == source)
    if status:
        q = q.where(Run.status == status)
    if since:
        q = q.where(Run.started_at >= since)
    total = session.scalar(select(func.count()).select_from(q.order_by(None).subquery()))
    rows = session.execute(
        q.order_by(Run.id.desc()).limit(page_size).offset((page - 1) * page_size)
    ).all()
    return {"items": [_run_dict(r, slug) for r, slug in rows], "total": total}


@router.get("/runs/{run_id}")
def get_run(run_id: int, session: Session = Depends(get_session)):
    run, slug = _run_or_404(session, run_id)
    top = session.execute(
        select(Change.indicator, func.count().label("n"))
        .where(Change.run_id == run_id)
        .group_by(Change.indicator)
        .order_by(func.count().desc(), Change.indicator)
        .limit(5)
    ).all()
    return {**_run_dict(run, slug), "top_indicators": [{"indicator": i, "count": n} for i, n in top]}


@router.get("/runs/{run_id}/changes")
def run_changes(
    run_id: int,
    change_type: Literal["added", "altered", "removed"] | None = None,
    indicator: str | None = None,
    country: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
):
    run, _ = _run_or_404(session, run_id)
    if run.status == "suspect_pending":
        try:
            items, total = pipeline.preview_changes(
                session, run_id, change_type, indicator, country, page, page_size
            )
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        return {
            "items": [{**i, "old": _json_value(i["old"]), "new": _json_value(i["new"])} for i in items],
            "total": total,
            "pending": True,
        }

    q = select(Change).where(Change.run_id == run_id)
    if change_type:
        q = q.where(Change.change_type == change_type)
    if indicator:
        q = q.where(Change.indicator == indicator)
    if country:
        q = q.where(Change.country == country)
    total = session.scalar(select(func.count()).select_from(q.order_by(None).subquery()))
    rows = session.scalars(q.order_by(Change.id).limit(page_size).offset((page - 1) * page_size))
    items = [
        {
            "change_type": c.change_type, "dataset": c.dataset, "indicator": c.indicator,
            "country": c.country, "period": c.period, "dims": c.dims,
            "old": _json_value(c.old_num if c.old_num is not None else c.old_text),
            "new": _json_value(c.new_num if c.new_num is not None else c.new_text),
        }
        for c in rows
    ]
    return {"items": items, "total": total}


@router.post("/runs/{run_id}/approve")
def approve(
    run_id: int,
    session: Session = Depends(get_session),
    session_factory: Callable[[], Session] = Depends(get_session_factory),
):
    _run_or_404(session, run_id)
    session.rollback()  # end this request's read transaction before the run takes locks
    try:
        run = approve_run(session_factory, run_id, notifier_from_settings())
    except SourceBusyError:
        raise HTTPException(409, "source busy") from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    return {"id": run.id, "status": run.status}


@router.post("/runs/{run_id}/reject")
def reject(
    run_id: int,
    session: Session = Depends(get_session),
    session_factory: Callable[[], Session] = Depends(get_session_factory),
):
    _run_or_404(session, run_id)
    session.rollback()
    try:
        run = reject_run(session_factory, run_id)
    except SourceBusyError:
        raise HTTPException(409, "source busy") from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    return {"id": run.id, "status": run.status}
