from datetime import datetime


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None
