"""Per-day change counts (change_rollup), so dashboards never scan `changes`."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

# UTC day, so the bucket does not depend on the session time zone.
_SELECT = """
    SELECT source_id, indicator, (created_at AT TIME ZONE 'UTC')::date AS day,
           count(*) FILTER (WHERE change_type = 'added'),
           count(*) FILTER (WHERE change_type = 'altered'),
           count(*) FILTER (WHERE change_type = 'removed')
    FROM changes {where}
    GROUP BY source_id, indicator, (created_at AT TIME ZONE 'UTC')::date
"""


def refresh_rollup(session: Session, run_id: int) -> None:
    """Recompute the full-day counts for every (source, indicator, day) this run touched.

    Idempotent: counts are overwritten from `changes`, so repeating the call changes nothing.
    """
    session.execute(
        text(
            """
            WITH touched AS (
                SELECT DISTINCT source_id, indicator, (created_at AT TIME ZONE 'UTC')::date AS day
                FROM changes WHERE run_id = :rid
            )
            INSERT INTO change_rollup (source_id, indicator, day, added, altered, removed)
            SELECT c.source_id, c.indicator, t.day,
                   count(*) FILTER (WHERE c.change_type = 'added'),
                   count(*) FILTER (WHERE c.change_type = 'altered'),
                   count(*) FILTER (WHERE c.change_type = 'removed')
            FROM touched t
            JOIN changes c ON c.source_id = t.source_id AND c.indicator = t.indicator
                          AND (c.created_at AT TIME ZONE 'UTC')::date = t.day
            GROUP BY c.source_id, c.indicator, t.day
            ON CONFLICT (source_id, indicator, day) DO UPDATE SET
                added = EXCLUDED.added, altered = EXCLUDED.altered, removed = EXCLUDED.removed
            """
        ),
        {"rid": run_id},
    )


def backfill(session: Session) -> int:
    """Rebuild the whole rollup from `changes`; returns the number of rollup rows."""
    session.execute(text("DELETE FROM change_rollup"))
    n = session.execute(
        text(
            "INSERT INTO change_rollup (source_id, indicator, day, added, altered, removed) "
            + _SELECT.format(where="")
        )
    ).rowcount
    session.commit()
    return n
