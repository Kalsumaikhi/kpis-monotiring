"""Key store API: secrets are write-only (never returned) and encrypted at rest."""

from __future__ import annotations

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
REQUIRED = {"header": "header_name", "query": "param_name", "basic": "username"}


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


def _validate(name: str | None, kind: str, fields: dict) -> None:
    if not (name or "").strip() or len(name.strip()) > 200:
        raise HTTPException(422, "name must be 1-200 characters")
    if not fields.get(REQUIRED[kind]):
        raise HTTPException(422, f"{REQUIRED[kind]} is required for a {kind} key")


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


def _readable(row: Credential) -> bool:
    try:
        creds.decrypt(row.secret_enc)
        return True
    except creds.KeyUnavailable:
        return False


def _json(row: Credential, used: dict | None) -> dict:
    return {
        "id": row.id, "name": row.name, "kind": row.kind, "header_name": row.header_name,
        "prefix": row.prefix, "param_name": row.param_name, "username": row.username,
        "has_secret": True, "readable": _readable(row),
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


@router.get("/credentials")
def list_credentials(session: Session = Depends(get_session)):
    rows = session.scalars(select(Credential).order_by(Credential.name)).all()
    used = _used_by(session)
    return [_json(r, used.get(r.id)) for r in rows]


@router.post("/credentials", status_code=201)
def create_credential(body: CredentialIn, session: Session = Depends(get_session)):
    fields = {k: _clean(getattr(body, k)) for k in ("header_name", "prefix", "param_name", "username")}
    _validate(body.name, body.kind, fields)
    if not body.secret:
        raise HTTPException(422, "secret is required")
    row = Credential(name=body.name.strip(), kind=body.kind, secret_enc=_encrypt(body.secret), **fields)
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "a key with this name already exists") from None
    session.refresh(row)
    return JSONResponse(_json(row, None), status_code=201)


@router.patch("/credentials/{cid}")
def patch_credential(cid: int, body: CredentialPatch, session: Session = Depends(get_session)):
    row = _get(session, cid)
    data = body.model_dump(exclude_unset=True)
    if "secret" in data and not data["secret"]:
        raise HTTPException(422, "secret must not be empty")
    for k in ("header_name", "prefix", "param_name", "username"):
        if k in data:
            data[k] = _clean(data[k])
    name = data.get("name", row.name)
    kind = data.get("kind", row.kind)
    fields = {k: data.get(k, getattr(row, k)) for k in ("header_name", "prefix", "param_name", "username")}
    if name is None or kind is None:
        raise HTTPException(422, "name and kind cannot be null")
    _validate(name, kind, fields)
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
    return _json(row, _used_by(session, [cid]).get(cid))


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
