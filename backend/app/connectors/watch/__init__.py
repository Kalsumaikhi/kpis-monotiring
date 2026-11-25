"""Watch sources: user-defined web pages and API links."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

NOISY_WINDOW = 10
NOISY_THRESHOLD = 8  # noisy = changed in MORE than this many of the last NOISY_WINDOW snapshots

_NOISY_SQL = """
SELECT t.source_id, t.id AS target_id
FROM watch_targets t
JOIN (
    SELECT target_id, count(*) FILTER (WHERE changed) AS n
    FROM (
        SELECT target_id, changed,
               row_number() OVER (PARTITION BY target_id ORDER BY id DESC) AS rn
        FROM watch_snapshots
    ) last
    WHERE rn <= :window
    GROUP BY target_id
) c ON c.target_id = t.id
WHERE c.n > :threshold {extra}
"""


def _noisy(session: Session, source_id: int | None):
    extra = "AND t.source_id = :sid" if source_id is not None else ""
    params = {"window": NOISY_WINDOW, "threshold": NOISY_THRESHOLD}
    if source_id is not None:
        params["sid"] = source_id
    return session.execute(text(_NOISY_SQL.format(extra=extra)), params).all()


def noisy_target_ids(session: Session, source_id: int) -> set[int]:
    """Targets that changed in more than 8 of their last 10 snapshots."""
    return {r.target_id for r in _noisy(session, source_id)}


def noisy_counts(session: Session) -> dict[int, int]:
    """Number of noisy targets per source id (only sources with at least one)."""
    counts: dict[int, int] = {}
    for r in _noisy(session, None):
        counts[r.source_id] = counts.get(r.source_id, 0) + 1
    return counts
