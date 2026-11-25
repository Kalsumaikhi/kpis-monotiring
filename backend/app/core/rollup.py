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
    """Add this run's changes to the per-day counts. Call once per applied run, in its transaction."""
    session.execute(
        text(
            "INSERT INTO change_rollup (source_id, indicator, day, added, altered, removed) "
            + _SELECT.format(where="WHERE run_id = :rid")
            + " ON CONFLICT (source_id, indicator, day) DO UPDATE SET "
              "added = change_rollup.added + EXCLUDED.added, "
              "altered = change_rollup.altered + EXCLUDED.altered, "
              "removed = change_rollup.removed + EXCLUDED.removed"
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
