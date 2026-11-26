from contextlib import contextmanager
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.pipeline import SourceBusyError, _acquire_source_lock


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


@contextmanager
def source_lock(session: Session, source_id: int, busy: str = "source is running, try again"):
    """Non-blocking per-source advisory lock (same as run_source); 409 while a run holds it."""
    try:
        release = _acquire_source_lock(session, source_id)
    except SourceBusyError:
        session.rollback()
        raise HTTPException(409, busy) from None
    try:
        yield
    finally:
        release()
