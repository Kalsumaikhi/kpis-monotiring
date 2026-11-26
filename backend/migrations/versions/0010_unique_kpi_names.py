"""watch_targets: KPI names are unique per source (case-insensitive)

Existing duplicates are renamed first, in id order: the oldest keeps its name, the others get
" (2)", " (3)", ... (skipping names already taken in the source). Every rename is logged.

A renamed KPI's stored rows were keyed by its old name, which another KPI may share, so they
are dropped (data_points and the indicator of that exact old name) and every KPI that used the
old name baselines silently on its next check: no "added" or "removed" alerts. The change
history (changes, change_rollup) is kept. Held runs of an affected source were diffed under the
old names, so they are superseded, as a KPI edit does.

Revision ID: 0010
Revises: 0009
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

INDEX = "uq_watch_targets_source_lower_kpi_name"
REASON = "superseded by a change to the source's KPIs (duplicate KPI names were renamed)"

log = logging.getLogger("alembic.runtime.migration")


def _renames(rows):
    """rows: (id, source_id, kpi_name) in id order. Returns [(id, source_id, old, new)]."""
    taken: dict[int, set[str]] = {}
    for _, sid, name in rows:
        taken.setdefault(sid, set()).add(name.lower())
    seen: set[tuple[int, str]] = set()
    out = []
    for tid, sid, name in rows:
        key = (sid, name.lower())
        if key not in seen:
            seen.add(key)
            continue
        n = 2
        while f"{name} ({n})".lower() in taken[sid]:
            n += 1
        new = f"{name} ({n})"
        taken[sid].add(new.lower())
        out.append((tid, sid, name, new))
    return out


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, source_id, kpi_name FROM watch_targets ORDER BY id")).all()
    renames = _renames([tuple(r) for r in rows])
    for tid, sid, old, new in renames:
        log.warning("0010: watch target %s in source %s renamed from %r to %r "
                    "(duplicate KPI name)", tid, sid, old, new)
        conn.execute(sa.text("UPDATE watch_targets SET kpi_name = :new, needs_baseline = true "
                             "WHERE id = :id"), {"new": new, "id": tid})
    for sid, old in {(sid, old) for _, sid, old, _ in renames}:
        conn.execute(sa.text(
            "DELETE FROM data_points WHERE source_id = :s AND indicator = :n "
            "AND dataset IN ('page', 'api')"), {"s": sid, "n": old})
        conn.execute(sa.text("DELETE FROM indicators WHERE source_id = :s AND code = :n"),
                     {"s": sid, "n": old})
        # a KPI that kept this exact name lost its rows too: baseline it
        conn.execute(sa.text("UPDATE watch_targets SET needs_baseline = true "
                             "WHERE source_id = :s AND kpi_name = :n"), {"s": sid, "n": old})
    for sid in sorted({sid for _, sid, _, _ in renames}):
        held = conn.execute(sa.text(
            "UPDATE runs SET status = 'superseded', error = :e "
            "WHERE source_id = :s AND status IN ('suspect_pending', 'approve_queued') "
            "RETURNING id"), {"s": sid, "e": REASON}).scalars().all()
        if held:
            conn.execute(sa.text("DELETE FROM pending_changes WHERE run_id = ANY(:ids)"),
                         {"ids": list(held)})
            log.warning("0010: held run(s) %s of source %s superseded", held, sid)
    op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX} "
               "ON watch_targets (source_id, lower(kpi_name))")


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
