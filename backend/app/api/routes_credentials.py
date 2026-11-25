"""Key store API: secrets are write-only (never returned) and encrypted at rest."""

from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import credentials as creds
from app.api.deps import get_session
from app.api.util import iso
from app.models import Credential, Source, WatchTarget

router = APIRouter()

KeyKind = Literal["header", "query", "basic"]
NO_KEY = "KPI_SECRET_KEY is not set — see README → Keys"
KIND_FIELDS = {"header": ("header_name", "prefix"), "query": ("param_name",), "basic": ("username",)}
ALL_FIELDS = ("header_name", "prefix", "param_name", "username")
TOKEN = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")


class CredentialIn(BaseModel):
    name: str
    kind: KeyKind
    header_name: str | None = None
    prefix: str | None = None
    param_name: str | None = None
    username: str | None = None
    secret: str


class CredentialPatch(BaseModel):
    name: str | None = None
    kind: KeyKind | None = None
    header_name: str | None = None
    prefix: str | None = None
    param_name: str | None = None
    username: str | None = None
    secret: str | None = None


def _clean(v: str | None) -> str | None:
    v = (v or "").strip()
    return v or None


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
    prefix, user = fields["prefix"], fields["username"]
    if prefix and (len(prefix) > 32 or re.search(r"[\r\n]", prefix)):
        raise HTTPException(422, "prefix must be at most 32 characters, no line breaks")
    if user and (":" in user or len(user) > 256):
        raise HTTPException(422, "username must be at most 256 characters and not contain ':'")
    if secret is not None:
        if not secret or len(secret) > 4096:
            raise HTTPException(422, "secret must be 1-4096 characters")
        if kind == "header" and re.search(r"[\r\n]", secret):
            raise HTTPException(422, "secret must not contain line breaks")
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


def _json(row: Credential, used: dict | None, fernet=None) -> dict:
    return {
        "id": row.id, "name": row.name, "kind": row.kind, "header_name": row.header_name,
        "prefix": row.prefix, "param_name": row.param_name, "username": row.username,
        "has_secret": True, "readable": _readable(row, fernet),
        "created_at": iso(row.created_at), "updated_at": iso(row.updated_at),
        "last_used_at": iso(row.last_used_at), "last_error": row.last_error,
        "used_by": used or {"targets": [], "sources": []},
    }


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


@router.get("/credentials/status")
def credentials_status():
    """Whether KPI_SECRET_KEY is set, so the UI can say so before anyone tries to save a key."""
    return {"configured": _fernet() is not None}


@router.get("/credentials")
def list_credentials(session: Session = Depends(get_session)):
    rows = session.scalars(select(Credential).order_by(Credential.name)).all()
    used = _used_by(session)
    f = _fernet()
    return [_json(r, used.get(r.id), f) for r in rows]


@router.post("/credentials", status_code=201)
def create_credential(body: CredentialIn, session: Session = Depends(get_session)):
    fields = {k: _clean(getattr(body, k)) for k in ALL_FIELDS}
    fields = _validate(body.name, body.kind, fields, body.secret)
    row = Credential(name=body.name.strip(), kind=body.kind, secret_enc=_encrypt(body.secret), **fields)
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "a key with this name already exists") from None
    session.refresh(row)
    return JSONResponse(_json(row, None, _fernet()), status_code=201)


@router.patch("/credentials/{cid}")
def patch_credential(cid: int, body: CredentialPatch, session: Session = Depends(get_session)):
    row = _get(session, cid)
    data = body.model_dump(exclude_unset=True)
    if "secret" in data and not data["secret"]:
        raise HTTPException(422, "secret must not be empty")
    for k in ALL_FIELDS:
        if k in data:
            data[k] = _clean(data[k])
    name = data.get("name", row.name)
    kind = data.get("kind", row.kind)
    fields = {k: data.get(k, getattr(row, k)) for k in ALL_FIELDS}
    if name is None or kind is None:
        raise HTTPException(422, "name and kind cannot be null")
    fields = _validate(name, kind, fields, data.get("secret"))
    if "secret" in data:
        row.secret_enc = _encrypt(data["secret"])
    row.name, row.kind = name.strip(), kind
    for k, v in fields.items():
        setattr(row, k, v)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "a key with this name already exists") from None
    session.refresh(row)
    return _json(row, _used_by(session, [cid]).get(cid), _fernet())


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
