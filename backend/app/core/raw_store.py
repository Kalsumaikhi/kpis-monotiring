"""On-disk store for downloaded raw files, one per run."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Run


def path_for(slug: str, run_id: int, suffix: str) -> Path:
    d = Path(get_settings().raw_dir) / slug
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{run_id}{suffix}"


def prune(session: Session, source_id: int, keep: int) -> None:
    """Delete raw files of runs older than the newest `keep`; never suspect_pending ones."""
    runs = session.scalars(
        select(Run)
        .where(Run.source_id == source_id, Run.raw_path.is_not(None))
        .order_by(Run.id.desc())
        .offset(keep)
    ).all()
    for r in runs:
        if r.status == "suspect_pending":
            continue
        Path(r.raw_path).unlink(missing_ok=True)
        r.raw_path = None
    session.commit()
