"""Shared dependencies: DB session, session factory and shared-password auth."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from app import db
from app.config import get_settings

_basic = HTTPBasic(auto_error=False)


def get_session() -> Iterator[Session]:
    with db.SessionLocal() as session:
        yield session


def get_session_factory() -> Callable[[], Session]:
    return db.SessionLocal


def require_auth(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    """Any username plus DASHBOARD_PASSWORD. An empty configured password never matches."""
    expected = get_settings().dashboard_password
    if not expected:
        raise HTTPException(503, "dashboard password not configured")
    if credentials is None or not secrets.compare_digest(
        credentials.password.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            401, "invalid credentials", headers={"WWW-Authenticate": 'Basic realm="kpi"'}
        )
