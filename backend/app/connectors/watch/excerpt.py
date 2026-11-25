"""Short added/removed line excerpt between two snapshots."""

from __future__ import annotations

import difflib

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import WatchSnapshot, WatchTarget

MAX_LINES = 5
MAX_CHARS = 200


def make_excerpt(old: str, new: str) -> dict[str, list[str]]:
    added: list[str] = []
    removed: list[str] = []
    for line in difflib.ndiff(old.splitlines(), new.splitlines()):
        if line.startswith("+ "):
            added.append(line[2:])
        elif line.startswith("- "):
            removed.append(line[2:])
    return {
        "added": [x[:MAX_CHARS] for x in added[:MAX_LINES]],
        "removed": [x[:MAX_CHARS] for x in removed[:MAX_LINES]],
    }


def page_excerpt(session: Session, source_id: int, kpi_name: str, run_id: int) -> dict[str, list[str]]:
    """Excerpt between the two latest snapshots of a page KPI at or before run_id.

    With a single snapshot (first fetch) everything counts as added.
    """
    contents = session.scalars(
        select(WatchSnapshot.content)
        .join(WatchTarget, WatchTarget.id == WatchSnapshot.target_id)
        .where(WatchTarget.source_id == source_id, WatchTarget.kpi_name == kpi_name,
               WatchSnapshot.run_id <= run_id)
        .order_by(WatchSnapshot.id.desc())
        .limit(2)
    ).all()
    new = contents[0] if contents else ""
    old = contents[1] if len(contents) > 1 else ""
    return make_excerpt(old, new)
