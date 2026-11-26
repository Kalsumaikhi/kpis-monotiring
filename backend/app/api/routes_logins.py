"""Interactive sign-in ("Sign in now") API. The worker does the browser work
(app.login_attempts); these endpoints start, watch and cancel attempts and take the emailed code,
which is stored encrypted until the worker types it in. No response ever carries the code."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import credentials as creds
from app.api.deps import get_session
from app.api.routes_credentials import NO_KEY
from app.api.util import iso
from app.login_attempts import CODE_WINDOW, EXPIRED
from app.models import ACTIVE_ATTEMPT_STATUSES, Credential, LoginAttempt

router = APIRouter()

CODE_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
BAD_CODE = "the code can only contain letters, digits and dashes"
IN_PROGRESS = "a sign-in is already in progress"
NOT_WAITING = "this sign-in is not waiting for a code"
FINISHED = "this sign-in has already finished"
NOT_FOUND = "sign-in not found"


class CodeIn(BaseModel):
    code: Any = None  # checked by hand, so a 422 never says anything about what was sent


def _login(session: Session, cid: int) -> Credential:
    row = session.get(Credential, cid)
    if row is None:
        raise HTTPException(404, "key not found")
    if row.kind != "login":
        raise HTTPException(422, "this key is not a login")
    return row


def _need_key() -> None:
    try:
        creds.get_fernet()
    except creds.KeyUnavailable:
        raise HTTPException(503, NO_KEY) from None


def _locked_attempt(session: Session, aid: int) -> LoginAttempt:
    a = session.scalars(select(LoginAttempt).where(LoginAttempt.id == aid)
                        .with_for_update()).first()
    if a is None:
        raise HTTPException(404, NOT_FOUND)
    return a


@router.post("/logins/{cid}/sign-in", status_code=202)
def start_sign_in(cid: int, session: Session = Depends(get_session)):
    _login(session, cid)
    _need_key()
    active = session.scalar(select(LoginAttempt.id).where(
        LoginAttempt.credential_id == cid, LoginAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES)))
    if active is not None:
        raise HTTPException(409, IN_PROGRESS)
    a = LoginAttempt(credential_id=cid, status="pending")
    session.add(a)
    try:
        session.commit()
    except IntegrityError:  # uq_login_attempts_active: started meanwhile
        session.rollback()
        raise HTTPException(409, IN_PROGRESS) from None
    return JSONResponse({"attempt_id": a.id, "status": a.status}, status_code=202)


@router.get("/logins/{cid}/attempt")
def latest_attempt(cid: int, session: Session = Depends(get_session)):
    _login(session, cid)
    a = session.scalars(select(LoginAttempt).where(LoginAttempt.credential_id == cid)
                        .order_by(LoginAttempt.id.desc()).limit(1)).first()
    if a is None:
        raise HTTPException(404, "no sign-in yet")
    return {"attempt_id": a.id, "status": a.status, "error": a.error,
            "waiting_since": iso(a.waiting_since),
            "expires_at": iso(a.waiting_since + CODE_WINDOW) if a.waiting_since else None}


@router.post("/logins/attempts/{aid}/code", status_code=202)
def submit_code(aid: int, body: CodeIn, session: Session = Depends(get_session)):
    code = body.code.strip() if isinstance(body.code, str) else None
    if not code or not CODE_RE.fullmatch(code):
        raise HTTPException(422, BAD_CODE)
    _need_key()
    a = _locked_attempt(session, aid)
    if a.status == "waiting_code":
        now = session.scalar(select(func.now()))
        if a.waiting_since is not None and now < a.waiting_since + CODE_WINDOW:
            a.code_enc = creds.encrypt(code)
            session.commit()
            return JSONResponse({"attempt_id": a.id, "status": a.status}, status_code=202)
        # Too late: the code is never kept.
        a.status, a.error, a.code_enc, a.finished_at = "expired", EXPIRED, None, func.now()
        session.commit()
        raise HTTPException(410, EXPIRED)
    session.rollback()
    if a.status == "expired":
        raise HTTPException(410, EXPIRED)
    raise HTTPException(409, NOT_WAITING)


@router.post("/logins/attempts/{aid}/cancel")
def cancel_attempt(aid: int, session: Session = Depends(get_session)):
    res = session.execute(
        update(LoginAttempt)
        .where(LoginAttempt.id == aid, LoginAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES))
        .values(status="cancelled", code_enc=None, finished_at=func.now()))
    session.commit()
    if res.rowcount:
        return {"attempt_id": aid, "status": "cancelled"}
    if session.get(LoginAttempt, aid) is None:
        raise HTTPException(404, NOT_FOUND)
    raise HTTPException(409, FINISHED)
