"""Key store API: secrets are write-only (never returned) and encrypted at rest."""

from __future__ import annotations

import re
from contextlib import ExitStack
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import credentials as creds
from app.api.deps import get_session
from app.api.util import iso, source_lock
from app.core.pipeline import _supersede_held
from app.api.routes_sources import KEY_CHANGE, _supports_keys
from app.models import ACTIVE_ATTEMPT_STATUSES, Credential, LoginAttempt, LoginSession, Source, WatchTarget

router = APIRouter()

KeyKind = Literal["header", "query", "basic", "login"]
NO_KEY = "KPI_SECRET_KEY is not set — see README → Keys"
KIND_FIELDS = {
    "header": ("header_name", "prefix"), "query": ("param_name",), "basic": ("username",),
    "login": ("login_url", "username", "selectors", "extra_hosts"),
}
ALL_FIELDS = ("header_name", "prefix", "param_name", "username", "login_url", "selectors", "extra_hosts")
SELECTOR_NAMES = ("username", "password", "submit", "code")
MAX_EXTRA_HOSTS = 10
HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
TOKEN = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
HEADER_SAFE = re.compile(r"^[\x20-\x7e]*$")  # printable ASCII: no CR/LF, no non-ASCII
MIN_SECRET = 8
NOT_ASCII = "header keys must be plain ASCII"


class CredentialIn(BaseModel):
    name: str
    kind: KeyKind
    header_name: str | None = None
    prefix: str | None = None
    param_name: str | None = None
    username: str | None = None
    login_url: str | None = None
    selectors: dict[str, Any] | None = None
    extra_hosts: list[Any] | None = None
    secret: str


class CredentialPatch(BaseModel):
    name: str | None = None
    kind: KeyKind | None = None
    header_name: str | None = None
    prefix: str | None = None
    param_name: str | None = None
    username: str | None = None
    login_url: str | None = None
    selectors: dict[str, Any] | None = None
    extra_hosts: list[Any] | None = None
    secret: str | None = None


def _clean(v):
    if isinstance(v, str):
        v = v.strip()
    return v or None


def _check_login(fields: dict) -> None:
    url = fields["login_url"]
    try:
        parts = urlsplit(url) if isinstance(url, str) else None
        ok = (parts is not None and parts.scheme.lower() == "https" and bool(parts.hostname)
              and len(url) <= 2048 and not re.search(r"[\x00-\x20\x7f]", url)
              and "@" not in parts.netloc)
        parts and parts.port  # raises ValueError on a bad port
    except ValueError:
        ok = False
    if not ok:
        raise HTTPException(422, "login page must be https")
    if not fields["username"]:
        raise HTTPException(422, "username is required")
    sel = fields["selectors"]
    if sel is not None:
        for name, v in sel.items():
            if (name not in SELECTOR_NAMES or not isinstance(v, str) or not v.strip()
                    or len(v) > 200 or re.search(r"[\r\n\x00]", v)):
                raise HTTPException(422, f"invalid selector: {name}")
        fields["selectors"] = {k: v.strip() for k, v in sel.items()} or None
    hosts = fields["extra_hosts"]
    if hosts is not None:
        if len(hosts) > MAX_EXTRA_HOSTS:
            raise HTTPException(422, f"at most {MAX_EXTRA_HOSTS} extra hosts")
        norm = []
        for h in hosts:
            n = h.strip().lower() if isinstance(h, str) else h
            if not isinstance(n, str) or not HOSTNAME.fullmatch(n):
                raise HTTPException(422, f"invalid host: {h}")
            norm.append(n)
        fields["extra_hosts"] = list(dict.fromkeys(norm)) or None


def _validate(name: str | None, kind: str, fields: dict, secret: str | None) -> dict:
    """Default and check the fields for `kind`; returns them with fields foreign to the kind nulled."""
    if not (name or "").strip() or len(name.strip()) > 200:
        raise HTTPException(422, "name must be 1-200 characters")
    fields = {k: (v if k in KIND_FIELDS[kind] else None) for k, v in fields.items()}
    if kind == "header":
        fields["header_name"] = fields["header_name"] or "Authorization"
    for k in ("header_name", "param_name"):
        v = fields[k]
        if kind == "query" and k == "param_name" and not v:
            raise HTTPException(422, "param_name is required for a query key")
        if v is not None and not TOKEN.match(v):
            raise HTTPException(422, f"{k} must be 1-64 letters, digits or token characters")
    if kind == "basic" and not fields["username"]:
        raise HTTPException(422, "username is required for a basic key")
    if kind == "login":
        _check_login(fields)
    prefix, user = fields["prefix"], fields["username"]
    if prefix and (len(prefix) > 32 or re.search(r"[\r\n]", prefix)):
        raise HTTPException(422, "prefix must be at most 32 characters, no line breaks")
    if kind == "header" and prefix and not HEADER_SAFE.match(prefix):
        raise HTTPException(422, NOT_ASCII)
    if user and ((kind == "basic" and ":" in user) or len(user) > 256):
        raise HTTPException(422, "username must be at most 256 characters and not contain ':'")
    if secret is not None:
        if len(secret) < MIN_SECRET:
            raise HTTPException(422, f"secret must be at least {MIN_SECRET} characters")
        if len(secret) > 4096:
            raise HTTPException(422, "secret must be at most 4096 characters")
        if kind == "header" and not HEADER_SAFE.match(secret):
            raise HTTPException(422, NOT_ASCII)
    return fields


def _used_by(session: Session, ids: list[int] | None = None) -> dict[int, dict]:
    out: dict[int, dict] = {}

    def slot(cid: int) -> dict:
        return out.setdefault(cid, {"targets": [], "sources": []})

    tq = (select(WatchTarget.credential_id, WatchTarget.id, WatchTarget.kpi_name, Source.name)
          .join(Source, Source.id == WatchTarget.source_id)
          .where(WatchTarget.credential_id.is_not(None)).order_by(WatchTarget.id))
    sq = (select(Source.credential_id, Source.slug)
          .where(Source.credential_id.is_not(None)).order_by(Source.id))
    if ids is not None:
        tq = tq.where(WatchTarget.credential_id.in_(ids))
        sq = sq.where(Source.credential_id.in_(ids))
    for cid, tid, kpi, source in session.execute(tq):
        slot(cid)["targets"].append({"id": tid, "kpi_name": kpi, "source": source})
    for cid, slug in session.execute(sq):
        slot(cid)["sources"].append(slug)
    return out


def _fernet():
    try:
        return creds.get_fernet()
    except creds.KeyUnavailable:
        return None


def _readable(row: Credential, fernet=None) -> bool:
    if fernet is None:
        return False
    try:
        creds.decrypt(row.secret_enc, fernet)
        return True
    except creds.KeyUnavailable:
        return False


def _login_info(session: Session, ids: list[int]) -> dict[int, tuple[str, str | None]]:
    if not ids:
        return {}
    q = select(LoginSession.credential_id, LoginSession.status, LoginSession.last_error)
    return {cid: (st, err) for cid, st, err in
            session.execute(q.where(LoginSession.credential_id.in_(ids)))}


def _json(row: Credential, used: dict | None, fernet=None, login: tuple | None = None) -> dict:
    out = {
        "id": row.id, "name": row.name, "kind": row.kind, "header_name": row.header_name,
        "prefix": row.prefix, "param_name": row.param_name, "username": row.username,
        "has_secret": True, "readable": _readable(row, fernet),
        "created_at": iso(row.created_at), "updated_at": iso(row.updated_at),
        "last_used_at": iso(row.last_used_at), "last_error": row.last_error,
        "used_by": used or {"targets": [], "sources": []},
        "login_url": row.login_url, "selectors": row.selectors, "extra_hosts": row.extra_hosts,
    }
    if row.kind == "login":
        out["login_status"], out["login_error"] = login or ("login_needed", None)
    return out


def _encrypt(secret: str) -> bytes:
    try:
        return creds.encrypt(secret)
    except creds.KeyUnavailable:
        raise HTTPException(503, NO_KEY) from None


def _get(session: Session, cid: int) -> Credential:
    row = session.get(Credential, cid)
    if row is None:
        raise HTTPException(404, "key not found")
    return row


def _cancel_attempts(session: Session, cid: int) -> None:
    """Cancel the login's active sign-in (the worker never overwrites a cancelled one)."""
    session.execute(
        update(LoginAttempt)
        .where(LoginAttempt.credential_id == cid, LoginAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES))
        .values(status="cancelled", code_enc=None, finished_at=func.now()))


@router.get("/credentials/status")
def credentials_status():
    """Whether KPI_SECRET_KEY is set, so the UI can say so before anyone tries to save a key."""
    return {"configured": _fernet() is not None}


@router.get("/credentials")
def list_credentials(session: Session = Depends(get_session)):
    rows = session.scalars(select(Credential).order_by(Credential.name)).all()
    used = _used_by(session)
    f = _fernet()
    info = _login_info(session, [r.id for r in rows if r.kind == "login"])
    return [_json(r, used.get(r.id), f, info.get(r.id)) for r in rows]


@router.post("/credentials", status_code=201)
def create_credential(body: CredentialIn, session: Session = Depends(get_session)):
    fields = {k: _clean(getattr(body, k)) for k in ALL_FIELDS}
    fields = _validate(body.name, body.kind, fields, body.secret)
    row = Credential(name=body.name.strip(), kind=body.kind, secret_enc=_encrypt(body.secret), **fields)
    session.add(row)
    try:
        session.flush()
        if row.kind == "login":
            session.add(LoginSession(credential_id=row.id, status="login_needed"))
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "a key with this name already exists") from None
    session.refresh(row)
    return JSONResponse(_json(row, None, _fernet(), _login_info(session, [row.id]).get(row.id)),
                        status_code=201)


@router.patch("/credentials/{cid}")
def patch_credential(cid: int, body: CredentialPatch, session: Session = Depends(get_session)):
    row = _get(session, cid)
    data = body.model_dump(exclude_unset=True)
    if "secret" in data and data["secret"] is None:
        raise HTTPException(422, f"secret must be at least {MIN_SECRET} characters")
    for k in ALL_FIELDS:
        if k in data:
            data[k] = _clean(data[k])
    name = data.get("name", row.name)
    kind = data.get("kind", row.kind)
    fields = {k: data.get(k, getattr(row, k)) for k in ALL_FIELDS}
    if name is None or kind is None:
        raise HTTPException(422, "name and kind cannot be null")
    fields = _validate(name, kind, fields, data.get("secret"))
    if row.kind == "login" and kind != "login" and session.scalar(
            select(WatchTarget.id).where(WatchTarget.credential_id == cid).limit(1)) is not None:
        # Its KPIs are browser KPIs on the login's hosts; another kind cannot serve them.
        raise HTTPException(409, "this login is used by KPIs")
    if row.kind != "login" and kind == "login" and _used_by(session, [cid]).get(cid):
        # Its KPIs and sources send it as a header/query/basic value; a login cannot.
        raise HTTPException(409, "this key is used by KPIs")
    if kind == "header" and row.kind != "header" and "secret" not in data:
        # The stored secret was never checked as a header value; check it now.
        try:
            stored = creds.decrypt(row.secret_enc, creds.get_fernet())
        except creds.KeyUnavailable:
            raise HTTPException(
                422, "this key's secret can't be read; enter a new secret to make it a header key"
            ) from None
        if not HEADER_SAFE.match(stored):
            raise HTTPException(422, NOT_ASCII)
    sends_change = _changes_what_is_sent(row, kind, fields, data)
    affected = _affected_sources(session, cid) if sends_change else []
    with ExitStack() as locks:
        # Try-locks only, in id order; a busy one releases those already held (409).
        for sid in affected:
            locks.enter_context(source_lock(session, sid, BUSY_KEY))
        if sends_change and not set(_affected_sources(session, cid)) <= set(affected):
            # The key was given to another source meanwhile; its lock is not held.
            session.rollback()
            raise HTTPException(409, BUSY_KEY)
        _patch(session, row, cid, data, name, kind, fields, affected)
    session.refresh(row)
    return _json(row, _used_by(session, [cid]).get(cid), _fernet(),
                 _login_info(session, [cid]).get(cid))


BUSY_KEY = "a check is running for a source that uses this key; try again when it finishes"
SENT_FIELDS = ("header_name", "prefix", "param_name", "username", "extra_hosts")


def _host(url: str | None) -> str | None:
    try:
        return (urlsplit(url).hostname or "").lower() or None if url else None
    except ValueError:
        return None


def _changes_what_is_sent(row: Credential, kind: str, fields: dict, data: dict) -> bool:
    """Whether the edit changes what is sent with the key, so data fetched with it may differ."""
    return ("secret" in data or kind != row.kind
            or any(fields[k] != getattr(row, k) for k in SENT_FIELDS)
            or _host(fields["login_url"]) != _host(row.login_url))


def _affected_sources(session: Session, cid: int) -> list[int]:
    """Sources whose fetched data depends on this key: those that send it as their own key
    (when the connector sends keys) and custom sources with KPIs that use it."""
    ids = {s.id for s in session.scalars(select(Source).where(Source.credential_id == cid))
           if _supports_keys(session, s)}
    ids |= set(session.scalars(
        select(WatchTarget.source_id).where(WatchTarget.credential_id == cid).distinct()))
    return sorted(ids)


def _patch(session: Session, row: Credential, cid: int, data: dict, name: str, kind: str,
           fields: dict, affected: list[int]) -> None:
    """Apply the edit and commit. With `affected` (whose locks the caller holds) the next run
    of each is a silent baseline for this key, and their held runs, diffed with the old
    key, are dropped."""
    reset = kind == "login" and row.kind == "login" and (
        "secret" in data or fields["username"] != row.username
        or fields["login_url"] != row.login_url)
    if "secret" in data:
        row.secret_enc = _encrypt(data["secret"])
        row.last_error = None  # a new secret has not been rejected yet; last_used_at stays
    row.name, row.kind = name.strip(), kind
    for k, v in fields.items():
        setattr(row, k, v)
    try:
        if kind == "login":
            ls = session.get(LoginSession, cid)
            if ls is None:
                session.add(LoginSession(credential_id=cid, status="login_needed"))
            elif reset:
                ls.status, ls.state_enc, ls.last_error = "login_needed", None, None
                ls.notified_at = None  # a new transition into login_needed alerts again
                _cancel_attempts(session, cid)
        else:
            ls = session.get(LoginSession, cid)
            if ls is not None:
                session.delete(ls)
                _cancel_attempts(session, cid)
        if affected:
            session.execute(update(Source).where(Source.id.in_(affected),
                                                 Source.credential_id == cid)
                            .values(needs_baseline=True))
            session.execute(update(WatchTarget).where(WatchTarget.credential_id == cid)
                            .values(needs_baseline=True))
            for sid in affected:
                _supersede_held(session, sid, reason=KEY_CHANGE)
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "a key with this name already exists") from None


@router.delete("/credentials/{cid}", status_code=204)
def delete_credential(cid: int, session: Session = Depends(get_session)):
    row = _get(session, cid)
    used = _used_by(session, [cid]).get(cid)
    if used:
        return JSONResponse({"detail": "key is in use", "used_by": used}, status_code=409)
    session.delete(row)
    try:
        session.commit()
    except IntegrityError:  # became used between the check and the delete
        session.rollback()
        raise HTTPException(409, "key is in use") from None
