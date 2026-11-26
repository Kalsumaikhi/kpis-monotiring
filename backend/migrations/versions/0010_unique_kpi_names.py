"""watch_targets: KPI names are unique per source (case-insensitive)

Existing duplicates are renamed first, in id order: the oldest keeps its name, the others get
" (2)", " (3)", ... (skipping names already taken in the source). Every rename is logged (and
written to stderr).

A watch KPI's stored rows (data_points, its indicator) are keyed by its exact name, so a
renamed KPI's rows are handled in one of two ways:

- Single owner (a case variant: "Price" and "PRICE"): only the renamed KPI used its exact old
  name, so its rows are re-keyed to the new name, the same way renaming a KPI in the app does
  (indicator and key_hash recomputed with the canonical formula, copied here so the migration
  does not depend on later app code). No re-baseline is needed.
- Exact-name clash (two KPIs both called "Price"): the rows under that name cannot be told
  apart, so they are dropped and every KPI that used the name baselines silently on its next
  check: no "added" or "removed" alerts.

The change history (changes, change_rollup) is kept either way. Held runs of an affected
source were diffed under the old names, so they are superseded with the same wording as a KPI
edit (pipeline._supersede_held).

Revision ID: 0010
Revises: 0009
"""
import hashlib
import json
import logging
import sys

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

INDEX = "uq_watch_targets_source_lower_kpi_name"
REASON = "a change to the source's KPIs"

log = logging.getLogger("alembic.runtime.migration")


def _say(line: str) -> None:
    if log.hasHandlers() and any(not isinstance(h, logging.NullHandler) for h in logging.getLogger().handlers + log.handlers):
        log.warning(line)
    else:
        print(line, file=sys.stderr)  # logging not configured: still show it


def _key_hash(dataset: str, indicator: str, country: str, period: str, dims) -> str:
    """app.core.canonical.key_hash_for(…, dims_to_json(dims)) as of this migration."""
    dims_json = json.dumps(dict(dims or {}), sort_keys=True, separators=(",", ":"))
    raw = "\x1f".join([dataset, indicator, country, period, dims_json])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


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


def _rekey(conn, sid: int, old: str, new: str) -> None:
    """Move the rows of a renamed KPI that alone used `old` to `new`."""
    conn.execute(sa.text("DELETE FROM indicators WHERE source_id = :s AND code = :n"),
                 {"s": sid, "n": new})  # a stale indicator of a deleted KPI may hold the name
    conn.execute(sa.text("UPDATE indicators SET code = :new, name = :new "
                         "WHERE source_id = :s AND code = :old"),
                 {"s": sid, "old": old, "new": new})
    points = conn.execute(sa.text(
        "SELECT id, dataset, country, period, dims FROM data_points "
        "WHERE source_id = :s AND indicator = :n AND dataset IN ('page', 'api')"),
        {"s": sid, "n": old}).all()
    for pid, dataset, country, period, dims in points:
        conn.execute(sa.text("UPDATE data_points SET indicator = :n, key_hash = :h WHERE id = :id"),
                     {"n": new, "h": _key_hash(dataset, new, country, period, dims), "id": pid})


def _drop(conn, sid: int, old: str) -> None:
    """Drop the rows under an exact name several KPIs shared; they all baseline again."""
    conn.execute(sa.text(
        "DELETE FROM data_points WHERE source_id = :s AND indicator = :n "
        "AND dataset IN ('page', 'api')"), {"s": sid, "n": old})
    conn.execute(sa.text("DELETE FROM indicators WHERE source_id = :s AND code = :n"),
                 {"s": sid, "n": old})


def upgrade() -> None:
    conn = op.get_bind()
    rows = [tuple(r) for r in conn.execute(sa.text(
        "SELECT id, source_id, kpi_name FROM watch_targets ORDER BY id")).all()]
    owners: dict[tuple[int, str], int] = {}
    for _, sid, name in rows:
        owners[(sid, name)] = owners.get((sid, name), 0) + 1
    renames = _renames(rows)
    for tid, sid, old, new in renames:
        single = owners[(sid, old)] == 1
        _say(f"0010: watch target {tid} in source {sid} renamed from {old!r} to {new!r} "
             f"(duplicate KPI name; {'rows re-keyed' if single else 'rows dropped, re-baseline'})")
        conn.execute(sa.text("UPDATE watch_targets SET kpi_name = :new WHERE id = :id"),
                     {"new": new, "id": tid})
        if single:
            _rekey(conn, sid, old, new)
        else:
            conn.execute(sa.text("UPDATE watch_targets SET needs_baseline = true WHERE id = :id"),
                         {"id": tid})
    for sid, old in {(sid, old) for _, sid, old, _ in renames if owners[(sid, old)] > 1}:
        _drop(conn, sid, old)
        # the KPI that kept this exact name lost its rows too: baseline it
        conn.execute(sa.text("UPDATE watch_targets SET needs_baseline = true "
                             "WHERE source_id = :s AND kpi_name = :n"), {"s": sid, "n": old})
    for sid in sorted({sid for _, sid, _, _ in renames}):
        # Same wording as pipeline._supersede_held (CASE reads the pre-update status).
        held = conn.execute(sa.text(
            "UPDATE runs SET status = 'superseded', error = CASE WHEN status = 'approve_queued' "
            "THEN :queued ELSE :plain END "
            "WHERE source_id = :s AND status IN ('suspect_pending', 'approve_queued') "
            "RETURNING id"),
            {"s": sid, "plain": f"superseded by {REASON}",
             "queued": f"superseded by {REASON} before the approval was applied"}
        ).scalars().all()
        if held:
            conn.execute(sa.text("DELETE FROM pending_changes WHERE run_id = ANY(:ids)"),
                         {"ids": list(held)})
            _say(f"0010: held run(s) {sorted(held)} of source {sid} superseded")
    op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX} "
               "ON watch_targets (source_id, lower(kpi_name))")


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
