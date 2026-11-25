"""Custom (watch) sources: CRUD for KPI targets, test link and Excel/CSV import."""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import watch_import
from app.api.deps import get_session
from app.api.util import iso
from app.connectors.watch import noisy_target_ids
from app.core.canonical import dims_to_json, key_hash_for
from app.connectors.watch.connector import _process
from app.connectors.watch.extract import flatten_json
from app.connectors.watch.fetch import fetch_target
from app.models import DataPoint, ImportPreview, Indicator, Source, WatchTarget

router = APIRouter()

MAX_UPLOAD = 2 * 1024 * 1024
PREVIEW_TTL = timedelta(hours=1)
WATCH_DATASETS = ("page", "api")
Kind = Literal["auto", "page", "api"]


def _valid_url(v: str) -> str:
    v = (v or "").strip()
    parts = urlsplit(v)
    if parts.scheme not in ("http", "https") or not parts.hostname or len(v) > 2048:
        raise ValueError("url must be an http(s) link")
    return v


def _nonblank(v: str) -> str:
    v = (v or "").strip()
    if not v or len(v) > 200:
        raise ValueError("must be 1-200 characters")
    return v


class TargetIn(BaseModel):
    source_name: str
    kpi_name: str
    url: str
    kind: Kind = "auto"
    selector: str | None = None
    use_browser: bool = False

    _n = field_validator("source_name", "kpi_name")(lambda cls, v: _nonblank(v))
    _u = field_validator("url")(lambda cls, v: _valid_url(v))


class TargetPatch(BaseModel):
    kpi_name: str | None = None
    url: str | None = None
    kind: Kind | None = None
    selector: str | None = None
    use_browser: bool | None = None
    enabled: bool | None = None

    @field_validator("kpi_name")
    @classmethod
    def _name(cls, v):
        return None if v is None else _nonblank(v)

    @field_validator("url")
    @classmethod
    def _url(cls, v):
        return None if v is None else _valid_url(v)


class TestIn(BaseModel):
    url: str
    kind: Kind = "auto"
    selector: str | None = None
    use_browser: bool = False

    _u = field_validator("url")(lambda cls, v: _valid_url(v))


class ConfirmIn(BaseModel):
    token: str


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "source"


def _target_json(t: WatchTarget, noisy: bool = False) -> dict:
    return {
        "id": t.id, "kpi_name": t.kpi_name, "url": t.url, "kind": t.kind,
        "selector": t.selector, "use_browser": t.use_browser, "enabled": t.enabled,
        "last_status": t.last_status, "last_checked_at": iso(t.last_checked_at),
        "last_error": t.last_error, "noisy": noisy,
    }


def _find_source(session: Session, name: str) -> Source | None:
    return session.scalars(
        select(Source).where(Source.access == "watch", Source.area == "custom",
                             func.lower(Source.name) == name.lower())
    ).first()


def _new_source(session: Session, name: str) -> Source:
    base = f"custom-{_slugify(name)}"
    slug, n = base, 1
    while session.scalar(select(Source.id).where(Source.slug == slug)) is not None:
        n += 1
        slug = f"{base}-{n}"
    src = Source(slug=slug, name=name, area="custom", access="watch")
    session.add(src)
    session.flush()
    return src


def _conflict(session: Session, source_id: int, kpi_name: str, url: str,
              exclude_id: int | None = None) -> str | None:
    q = select(WatchTarget).where(WatchTarget.source_id == source_id)
    if exclude_id is not None:
        q = q.where(WatchTarget.id != exclude_id)
    for t in session.scalars(q.where((WatchTarget.url == url) | (WatchTarget.kpi_name == kpi_name))):
        return "a KPI with this link already exists" if t.url == url else \
            "a KPI with this name already exists in the source"
    return None


def _delete_points(session: Session, source_id: int, kpi_name: str) -> None:
    session.execute(delete(DataPoint).where(
        DataPoint.source_id == source_id, DataPoint.dataset.in_(WATCH_DATASETS),
        DataPoint.indicator == kpi_name))


def _add_target(session: Session, source: Source, kpi_name: str, url: str, kind="auto",
                selector=None, use_browser=False) -> WatchTarget:
    t = WatchTarget(source_id=source.id, kpi_name=kpi_name, url=url, kind=kind,
                    selector=(selector or None), use_browser=use_browser, needs_baseline=True)
    session.add(t)
    session.flush()
    return t


@router.get("/custom-sources")
def list_custom_sources(session: Session = Depends(get_session)):
    out = []
    for s in session.scalars(select(Source).where(Source.access == "watch").order_by(Source.name)):
        noisy = noisy_target_ids(session, s.id)
        targets = session.scalars(
            select(WatchTarget).where(WatchTarget.source_id == s.id).order_by(WatchTarget.id)).all()
        out.append({"slug": s.slug, "name": s.name, "enabled": s.enabled, "cron": s.cron,
                    "targets": [_target_json(t, t.id in noisy) for t in targets]})
    return out


@router.post("/custom-sources", status_code=201)
def create_target(body: TargetIn, session: Session = Depends(get_session)):
    source = _find_source(session, body.source_name)
    if source is not None:
        msg = _conflict(session, source.id, body.kpi_name, body.url)
        if msg:
            raise HTTPException(409, msg)
    try:
        if source is None:
            source = _new_source(session, body.source_name)
        t = _add_target(session, source, body.kpi_name, body.url, body.kind, body.selector,
                        body.use_browser)
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "conflicting source or KPI") from None
    return _target_json(t)


def _target_or_404(session: Session, target_id: int) -> WatchTarget:
    t = session.get(WatchTarget, target_id)
    if t is None:
        raise HTTPException(404, "target not found")
    return t


@router.patch("/watch-targets/{target_id}")
def patch_target(target_id: int, body: TargetPatch, session: Session = Depends(get_session)):
    t = _target_or_404(session, target_id)
    new_name = body.kpi_name if body.kpi_name is not None else t.kpi_name
    new_url = body.url if body.url is not None else t.url
    if (new_name, new_url) != (t.kpi_name, t.url):
        msg = _conflict(session, t.source_id, new_name, new_url, exclude_id=t.id)
        if msg:
            raise HTTPException(409, msg)
    old_name = t.kpi_name
    if body.url is not None and new_url != t.url:
        _delete_points(session, t.source_id, old_name)  # a new link is a new baseline
        t.url, t.needs_baseline = new_url, True
        t.last_status, t.last_checked_at, t.last_error = "never", None, None
    if new_name != old_name:
        # indicator is part of key_hash, so the hash is recomputed (same formula as canonical)
        for dp in session.scalars(select(DataPoint).where(
                DataPoint.source_id == t.source_id, DataPoint.dataset.in_(WATCH_DATASETS),
                DataPoint.indicator == old_name)):
            dp.indicator = new_name
            dp.key_hash = key_hash_for(dp.dataset, new_name, dp.country, dp.period,
                                       dims_to_json(dp.dims))
        session.execute(update(Indicator).where(
            Indicator.source_id == t.source_id, Indicator.code == old_name
        ).values(code=new_name, name=new_name))
        t.kpi_name = new_name
    if body.enabled is not None and body.enabled != t.enabled:
        if not body.enabled:
            _delete_points(session, t.source_id, t.kpi_name)
            t.needs_baseline = True
        t.enabled = body.enabled
    for f in ("kind", "selector", "use_browser"):
        if f in body.model_fields_set and getattr(body, f) is not None:
            setattr(t, f, getattr(body, f))
    if "selector" in body.model_fields_set and body.selector is None:
        t.selector = None
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "conflicting KPI") from None
    return _target_json(t)


@router.delete("/watch-targets/{target_id}", status_code=204)
def delete_target(target_id: int, session: Session = Depends(get_session)):
    t = _target_or_404(session, target_id)
    _delete_points(session, t.source_id, t.kpi_name)
    session.delete(t)
    session.commit()


@router.post("/watch-targets/test")
def test_link(body: TestIn):
    """Fetch and extract once; nothing is written."""
    entry = {"id": 0, "kpi_name": "", "url": body.url, "kind": body.kind,
             "selector": body.selector or None}
    with httpx.Client(timeout=30) as client:
        try:
            res = fetch_target(client, body.url, body.use_browser)
            e = _process(entry, res)
        except Exception as exc:  # noqa: BLE001 - report, never 500
            return {"status": "error", "kind": body.kind, "error": f"unexpected error: {type(exc).__name__}"}
    out: dict = {"status": e["status"], "kind": e.get("kind", body.kind)}
    if e["status"] != "ok":
        out["error"] = e.get("error")
    elif "json" in e:
        out["json_preview"] = [{"path": p, "value": v}
                               for p, v in list(flatten_json(e["json"]).items())[:50]]
    else:
        out["text_preview"] = e["text"][:2000]
    return out


# --- import ---------------------------------------------------------------------

def _classify(session: Session, rows: list[dict]) -> list[dict]:
    """Add status/error to each sheet row."""
    seen_urls: set[tuple[str, str]] = set()
    seen_names: set[tuple[str, str]] = set()
    known_sources: dict[str, Source | None] = {}
    out = []
    for r in rows:
        res = {**r}
        src_key = r["source_name"].lower()
        try:
            _nonblank(r["source_name"])
            _nonblank(r["kpi_name"])
            _valid_url(r["url"])
        except ValueError as e:
            msg = str(e)
            if not r["source_name"].strip() or not r["kpi_name"].strip():
                msg = "source name and KPI name are required"
            res.update(status="invalid", error=msg)
            out.append(res)
            continue
        if src_key not in known_sources:
            known_sources[src_key] = _find_source(session, r["source_name"])
        src = known_sources[src_key]
        dup = (src_key, r["url"]) in seen_urls or (src_key, r["kpi_name"].lower()) in seen_names
        if src is not None and not dup:
            dup = any(
                t.url == r["url"] or t.kpi_name.lower() == r["kpi_name"].lower()
                for t in session.scalars(select(WatchTarget).where(WatchTarget.source_id == src.id)))
        if dup:
            res["status"] = "duplicate"
        else:
            seen_urls.add((src_key, r["url"]))
            seen_names.add((src_key, r["kpi_name"].lower()))
            res["status"] = "existing_source" if src is not None else "new_source"
        out.append(res)
    return out


@router.post("/watch-targets/import/preview")
async def import_preview(file: UploadFile = File(...), session: Session = Depends(get_session)):
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "file too large (max 2 MB)")
    try:
        rows = watch_import.parse_sheet(file.filename or "", data)
    except watch_import.SheetError as e:
        raise HTTPException(422, str(e)) from None
    classified = _classify(session, rows)
    session.execute(delete(ImportPreview).where(
        ImportPreview.created_at < datetime.now(timezone.utc) - PREVIEW_TTL))
    token = secrets.token_urlsafe(24)
    session.add(ImportPreview(token=token, rows=classified))
    session.commit()
    return {"token": token, "rows": classified}


@router.post("/watch-targets/import/confirm")
def import_confirm(body: ConfirmIn, session: Session = Depends(get_session)):
    # DELETE ... RETURNING makes the token single-use even with concurrent confirms.
    got = session.execute(
        delete(ImportPreview).where(ImportPreview.token == body.token)
        .returning(ImportPreview.rows, ImportPreview.created_at)).first()
    if got is None:
        session.rollback()
        raise HTTPException(410, "import preview not found or already used")
    rows, created_at = got
    if datetime.now(timezone.utc) - created_at > PREVIEW_TTL:
        session.commit()
        raise HTTPException(410, "import preview expired")
    created_sources = created_targets = skipped = 0
    for r in rows:
        if r["status"] not in ("new_source", "existing_source"):
            skipped += 1
            continue
        source = _find_source(session, r["source_name"])
        if source is not None and _conflict(session, source.id, r["kpi_name"], r["url"]):
            skipped += 1  # created since the preview
            continue
        if source is None:
            source = _new_source(session, r["source_name"])
            created_sources += 1
        _add_target(session, source, r["kpi_name"], r["url"])
        created_targets += 1
    session.commit()
    return {"created_sources": created_sources, "created_targets": created_targets,
            "skipped": skipped}
