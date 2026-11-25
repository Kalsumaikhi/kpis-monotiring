"""KPI change frequency and the source -> indicator -> country explorer."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.util import iso
from app.connectors.registry import CONNECTORS

router = APIRouter()

SORTS = {
    "changes_30d": "changes_30d",
    "changes_12m": "changes_12m",
    "last_changed": "last_changed",
    "typical_interval": "typical_interval_days",
}
HISTORY_LIMIT = 500


def _like(q: str | None) -> str | None:
    if not q:
        return None
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def _source_id_or_404(session: Session, slug: str) -> int:
    sid = session.execute(text("SELECT id FROM sources WHERE slug = :s"), {"s": slug}).scalar()
    if sid is None:
        raise HTTPException(404, "source not found")
    return sid


def attribution_for(slug: str) -> str | None:
    return getattr(CONNECTORS.get(slug), "attribution", None)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _month_start(today: date, back: int) -> date:
    idx = today.year * 12 + today.month - 1 - back
    return date(idx // 12, idx % 12 + 1, 1)


# Everything below reads change_rollup (one row per indicator and day with changes), never `changes`.
_KPI_SQL = """
WITH gaps AS (
    SELECT source_id, indicator,
           day - lag(day) OVER (PARTITION BY source_id, indicator ORDER BY day) AS gap
    FROM change_rollup
), med AS (
    SELECT source_id, indicator, percentile_cont(0.5) WITHIN GROUP (ORDER BY gap) AS typical
    FROM gaps WHERE gap IS NOT NULL GROUP BY source_id, indicator
), agg AS (
    SELECT source_id, indicator,
           sum(added + altered + removed) AS changes_all,
           coalesce(sum(added + altered + removed) FILTER (WHERE day >= :since12), 0) AS changes_12m,
           coalesce(sum(added + altered + removed) FILTER (WHERE day >= :since30), 0) AS changes_30d,
           max(day) AS last_changed
    FROM change_rollup GROUP BY source_id, indicator
)
SELECT s.slug AS source, i.source_id, i.code AS indicator, coalesce(i.name, i.code) AS name,
       i.unit, i.first_seen_at AS first_seen,
       coalesce(a.changes_all, 0) AS changes_all, coalesce(a.changes_12m, 0) AS changes_12m,
       coalesce(a.changes_30d, 0) AS changes_30d, a.last_changed,
       m.typical AS typical_interval_days,
       count(*) OVER () AS total
FROM indicators i
JOIN sources s ON s.id = i.source_id
LEFT JOIN agg a ON a.source_id = i.source_id AND a.indicator = i.code
LEFT JOIN med m ON m.source_id = i.source_id AND m.indicator = i.code
WHERE (CAST(:slug AS text) IS NULL OR s.slug = :slug)
  AND (CAST(:q AS text) IS NULL OR i.code ILIKE :q OR i.name ILIKE :q)
ORDER BY {sort} {order} NULLS LAST, s.slug, i.code
LIMIT :limit OFFSET :offset
"""


@router.get("/kpis")
def list_kpis(
    source: str | None = None,
    q: str | None = None,
    sort: Literal["changes_30d", "changes_12m", "last_changed", "typical_interval"] = "changes_30d",
    order: Literal["desc", "asc"] = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
):
    if source is not None:
        _source_id_or_404(session, source)
    today = _today()
    first_month = _month_start(today, 11)
    rows = session.execute(
        text(_KPI_SQL.format(sort=SORTS[sort], order="DESC" if order == "desc" else "ASC")),
        {"slug": source, "q": _like(q), "since30": date.fromordinal(today.toordinal() - 30),
         "since12": first_month, "limit": page_size, "offset": (page - 1) * page_size},
    ).all()
    spark: dict[tuple[int, str], list[int]] = {}
    if rows:
        monthly = session.execute(
            text(
                """
                SELECT source_id, indicator, date_trunc('month', day)::date AS month,
                       sum(added + altered + removed) AS n
                FROM change_rollup
                WHERE source_id = ANY(:sids) AND indicator = ANY(:codes) AND day >= :first
                GROUP BY source_id, indicator, date_trunc('month', day)
                """
            ),
            {"sids": list({r.source_id for r in rows}), "codes": [r.indicator for r in rows],
             "first": first_month},
        ).all()
        months = [_month_start(today, back) for back in range(11, -1, -1)]
        pos = {m: n for n, m in enumerate(months)}
        for r in rows:
            spark[(r.source_id, r.indicator)] = [0] * 12
        for sid, code, month, n in monthly:
            if (sid, code) in spark and month in pos:
                spark[(sid, code)][pos[month]] = int(n)
    return {
        "items": [
            {
                "source": r.source,
                "indicator": r.indicator,
                "name": r.name,
                "unit": r.unit,
                "changes_30d": int(r.changes_30d),
                "changes_12m": int(r.changes_12m),
                "changes_all": int(r.changes_all),
                "first_seen": iso(r.first_seen),
                "last_changed": r.last_changed.isoformat() if r.last_changed else None,
                "typical_interval_days": (
                    None if r.typical_interval_days is None else float(r.typical_interval_days)
                ),
                "sparkline": spark[(r.source_id, r.indicator)],
            }
            for r in rows
        ],
        "total": rows[0].total if rows else _kpi_total(session, source, q),
    }


def _kpi_total(session: Session, slug: str | None, q: str | None) -> int:
    """Total when the requested page is empty (past the end), so window count is unavailable."""
    return session.execute(
        text(
            """
            SELECT count(*) FROM indicators i JOIN sources s ON s.id = i.source_id
            WHERE (CAST(:slug AS text) IS NULL OR s.slug = :slug)
              AND (CAST(:q AS text) IS NULL OR i.code ILIKE :q OR i.name ILIKE :q)
            """
        ),
        {"slug": slug, "q": _like(q)},
    ).scalar_one()


@router.get("/sources/{slug}/indicators")
def list_indicators(
    slug: str,
    q: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
):
    sid = _source_id_or_404(session, slug)
    params = {"sid": sid, "q": _like(q)}
    where = "source_id = :sid AND (CAST(:q AS text) IS NULL OR code ILIKE :q OR name ILIKE :q)"
    total = session.execute(text(f"SELECT count(*) FROM indicators WHERE {where}"), params).scalar_one()
    rows = session.execute(
        text(f"SELECT code, name, unit FROM indicators WHERE {where} "
             "ORDER BY code LIMIT :limit OFFSET :offset"),
        {**params, "limit": page_size, "offset": (page - 1) * page_size},
    ).all()
    counts: dict[str, int] = {}
    if rows:  # one grouped query per page, on the (source_id, indicator) index
        counts = dict(
            session.execute(
                text("SELECT indicator, count(*) FROM data_points "
                     "WHERE source_id = :sid AND indicator = ANY(:codes) GROUP BY indicator"),
                {"sid": sid, "codes": [r.code for r in rows]},
            ).all()
        )
    return {
        "items": [{"code": r.code, "name": r.name, "unit": r.unit, "points": counts.get(r.code, 0)}
                  for r in rows],
        "total": total,
    }


# {code:path}: custom KPI names may contain "/" (the client sends it as %2F, which the
# server decodes before routing).
@router.get("/sources/{slug}/indicators/{code:path}/countries")
def list_countries(
    slug: str,
    code: str,
    q: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
):
    sid = _source_id_or_404(session, slug)
    rows = session.execute(
        text(
            """
            SELECT country, count(*) AS points, max(last_changed_at) AS last_changed,
                   count(*) OVER () AS total
            FROM data_points
            WHERE source_id = :sid AND indicator = :code
              AND (CAST(:q AS text) IS NULL OR country ILIKE :q)
            GROUP BY country
            ORDER BY country
            LIMIT :limit OFFSET :offset
            """
        ),
        {"sid": sid, "code": code, "q": _like(q), "limit": page_size,
         "offset": (page - 1) * page_size},
    ).all()
    total = rows[0].total if rows else 0
    if not rows and page > 1:
        total = session.execute(
            text("SELECT count(DISTINCT country) FROM data_points "
                 "WHERE source_id = :sid AND indicator = :code "
                 "AND (CAST(:q AS text) IS NULL OR country ILIKE :q)"),
            {"sid": sid, "code": code, "q": _like(q)},
        ).scalar_one()
    return {
        "items": [{"country": r.country, "points": r.points, "last_changed": iso(r.last_changed)}
                  for r in rows],
        "total": total,
    }


def _value(num, txt) -> str | None:
    if txt is not None:
        return txt
    return None if num is None else format(num, "f")


@router.get("/sources/{slug}/series")
def series(
    slug: str,
    indicator: str,
    country: str = "",  # watch (custom) sources have no country; the client omits empty values
    session: Session = Depends(get_session),
):
    sid = _source_id_or_404(session, slug)
    params = {"sid": sid, "ind": indicator, "c": country}
    points = session.execute(
        text("SELECT period, dims, value_num, value_text FROM data_points "
             "WHERE source_id = :sid AND indicator = :ind AND country = :c "
             "ORDER BY period, dims::text"),
        params,
    ).all()
    history = session.execute(
        text(
            """
            SELECT run_id, created_at, change_type, period, dims,
                   old_num, old_text, new_num, new_text
            FROM changes
            WHERE source_id = :sid AND indicator = :ind AND country = :c
            ORDER BY created_at DESC, id DESC LIMIT :lim
            """
        ),
        {**params, "lim": HISTORY_LIMIT},
    ).all()
    return {
        "points": [{"period": p.period, "dims": p.dims, "value": _value(p.value_num, p.value_text)}
                   for p in points],
        "history": [
            {"run_id": h.run_id, "created_at": iso(h.created_at), "change_type": h.change_type,
             "period": h.period, "dims": h.dims,
             "old": _value(h.old_num, h.old_text), "new": _value(h.new_num, h.new_text)}
            for h in history
        ],
        "attribution": attribution_for(slug),
    }
