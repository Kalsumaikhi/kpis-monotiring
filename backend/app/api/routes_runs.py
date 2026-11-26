from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.util import iso
from app.api.deps import get_session, get_session_factory
from app.connectors.watch.excerpt import page_excerpt
from app.core.pipeline import SourceBusyError, reject_run
from app.models import Change, PendingChange, Run, Source

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


# The distinct sources.area values: the connectors' areas plus "custom" for watch sources.
Area = Literal["economic", "military", "custom"]
ChangeKind = Literal["added", "altered", "removed"]


@router.get("/runs")
def list_runs(
    source: str | None = None,
    status: str | None = None,
    since: datetime | None = None,
    has_changes: bool = False,
    area: Area | None = None,
    change: ChangeKind | None = None,
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
    if has_changes:
        q = q.where(Run.added + Run.altered + Run.removed > 0)
    if area:
        q = q.where(Source.area == area)
    if change:
        q = q.where(getattr(Run, change) > 0)
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
    held = run.status in ("suspect_pending", "approve_queued")
    model = PendingChange if held else Change
    q = select(model).where(model.run_id == run_id)
    if change_type:
        q = q.where(model.change_type == change_type)
    if indicator:
        q = q.where(model.indicator == indicator)
    if country:
        q = q.where(model.country == country)
    total = session.scalar(select(func.count()).select_from(q.order_by(None).subquery()))
    rows = session.scalars(q.order_by(model.id).limit(page_size).offset((page - 1) * page_size))
    rows = list(rows)
    excerpts: dict[str, dict] = {}  # one excerpt per KPI, not per row

    def excerpt_of(c) -> dict:
        if c.indicator not in excerpts:
            excerpts[c.indicator] = page_excerpt(session, run.source_id, c.indicator, run_id)
        return excerpts[c.indicator]

    items = [
        {
            "change_type": c.change_type, "dataset": c.dataset, "indicator": c.indicator,
            "country": c.country, "period": c.period, "dims": c.dims,
            "old": _json_value(c.old_num if c.old_num is not None else c.old_text),
            "new": _json_value(c.new_num if c.new_num is not None else c.new_text),
            **({"excerpt": excerpt_of(c)} if c.dataset == "page" else {}),
        }
        for c in rows
    ]
    return {"items": items, "total": total, **({"pending": True} if held else {})}


@router.post("/runs/{run_id}/approve", status_code=202)
def approve(run_id: int, session: Session = Depends(get_session)):
    """Queue the approval; the worker applies it. Only the latest held run can be queued."""
    run, _ = _run_or_404(session, run_id)
    latest = (
        select(func.max(Run.id))
        .where(Run.source_id == run.source_id, Run.status == "suspect_pending")
        .scalar_subquery()
    )
    claimed = session.execute(
        update(Run)
        .where(Run.id == run_id, Run.status == "suspect_pending", Run.id == latest)
        .values(status="approve_queued")
    ).rowcount
    session.commit()
    if claimed == 0:
        raise HTTPException(409, f"run {run_id} is not the latest suspect_pending run")
    return {"run_id": run_id, "status": "approve_queued"}


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
