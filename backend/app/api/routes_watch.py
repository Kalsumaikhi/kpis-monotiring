"""Custom (watch) sources: CRUD for KPI targets, test link and Excel/CSV import."""

from __future__ import annotations

import re
import secrets
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
from app.api.util import iso, source_lock as _source_lock
from app.connectors.watch import noisy_target_ids
from app.core.pipeline import _supersede_held
from app.core.canonical import dims_to_json, key_hash_for
from app.connectors.watch.connector import _process
from app.connectors.watch.extract import flatten_json
from app import credentials as creds
from app import logins
from app.connectors.watch.fetch import (
    NEEDS_HTTPS, NO_BROWSER_KEY, FetchState, fetch_target, preflight,
)
from app.config import get_settings
from app.models import (
    Change, ChangeRollup, Credential, DataPoint, ImportPreview, Indicator, PendingChange, Run,
    Source, Upload, WatchTarget,
)

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
    credential_id: int | None = None

    _n = field_validator("source_name", "kpi_name")(lambda cls, v: _nonblank(v))
    _u = field_validator("url")(lambda cls, v: _valid_url(v))


class TargetPatch(BaseModel):
    kpi_name: str | None = None
    url: str | None = None
    kind: Kind | None = None
    selector: str | None = None
    use_browser: bool | None = None
    enabled: bool | None = None
    credential_id: int | None = None

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
    credential_id: int | None = None

    _u = field_validator("url")(lambda cls, v: _valid_url(v))


class ConfirmIn(BaseModel):
    token: str


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "source"


def _check_key(session: Session, credential_id: int | None, url: str, use_browser: bool) -> bool:
    """422 unless the key exists and the link is one a key may be sent to. Returns the
    browser mode to store: a login key is always fetched in the browser (and only on its
    allowed hosts); other keys cannot be used with it."""
    if credential_id is None:
        return use_browser
    row = session.get(Credential, credential_id)
    if row is None:
        raise HTTPException(422, "unknown key")
    if urlsplit(url).scheme != "https":
        raise HTTPException(422, NEEDS_HTTPS)
    if row.kind == "login":
        err = creds.login_host_error(row, url)
        if err:
            raise HTTPException(422, err)
        return True
    if use_browser:
        raise HTTPException(422, NO_BROWSER_KEY)
    return False


def _is_login(session: Session, credential_id: int | None) -> bool:
    return credential_id is not None and session.scalar(
        select(Credential.kind).where(Credential.id == credential_id)) == "login"


def _key_name(session: Session, credential_id: int | None) -> str | None:
    return None if credential_id is None else session.scalar(
        select(Credential.name).where(Credential.id == credential_id))


def _target_json(t: WatchTarget, noisy: bool = False, key_name: str | None = None) -> dict:
    return {
        "id": t.id, "kpi_name": t.kpi_name, "url": t.url, "kind": t.kind,
        "selector": t.selector, "use_browser": t.use_browser, "enabled": t.enabled,
        "last_status": t.last_status, "last_checked_at": iso(t.last_checked_at),
        "last_error": t.last_error, "noisy": noisy,
        "credential_id": t.credential_id, "credential_name": key_name,
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
    for t in session.scalars(q.where((WatchTarget.url == url) | (func.lower(WatchTarget.kpi_name) == kpi_name.lower()))):
        return "a KPI with this link already exists" if t.url == url else \
            "a KPI with this name already exists in the source"
    return None


def _delete_points(session: Session, source_id: int, kpi_name: str) -> None:
    session.execute(delete(DataPoint).where(
        DataPoint.source_id == source_id, DataPoint.dataset.in_(WATCH_DATASETS),
        DataPoint.indicator == kpi_name))


def _add_target(session: Session, source: Source, kpi_name: str, url: str, kind="auto",
                selector=None, use_browser=False, credential_id=None) -> WatchTarget:
    t = WatchTarget(source_id=source.id, kpi_name=kpi_name, url=url, kind=kind,
                    selector=(selector or None), use_browser=use_browser, needs_baseline=True,
                    credential_id=credential_id)
    session.add(t)
    session.flush()
    return t


@router.get("/custom-sources")
def list_custom_sources(session: Session = Depends(get_session)):
    out = []
    for s in session.scalars(select(Source).where(Source.access == "watch").order_by(Source.name)):
        noisy = noisy_target_ids(session, s.id)
        targets = session.execute(
            select(WatchTarget, Credential.name)
            .outerjoin(Credential, Credential.id == WatchTarget.credential_id)
            .where(WatchTarget.source_id == s.id).order_by(WatchTarget.id)).all()
        out.append({"slug": s.slug, "name": s.name, "enabled": s.enabled, "cron": s.cron,
                    "targets": [_target_json(t, t.id in noisy, name) for t, name in targets]})
    return out


@router.post("/custom-sources", status_code=201)
def create_target(body: TargetIn, session: Session = Depends(get_session)):
    use_browser = _check_key(session, body.credential_id, body.url, body.use_browser)
    source = _find_source(session, body.source_name)
    if source is not None:
        msg = _conflict(session, source.id, body.kpi_name, body.url)
        if msg:
            raise HTTPException(409, msg)
    try:
        if source is None:
            source = _new_source(session, body.source_name)
        t = _add_target(session, source, body.kpi_name, body.url, body.kind, body.selector,
                        use_browser, body.credential_id)
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "conflicting source or KPI") from None
    return _target_json(t, key_name=_key_name(session, t.credential_id))


def _target_or_404(session: Session, target_id: int) -> WatchTarget:
    t = session.get(WatchTarget, target_id)
    if t is None:
        raise HTTPException(404, "target not found")
    return t


@router.delete("/custom-sources/{slug}", status_code=204)
def delete_custom_source(slug: str, session: Session = Depends(get_session)):
    """Remove a custom source with all its rows and raw files. Silent: no Mattermost post.
    Keys and logins are shared and stay."""
    src = session.scalar(select(Source).where(
        Source.slug == slug, Source.access == "watch", Source.area == "custom"))
    if src is None:
        raise HTTPException(404, "custom source not found")
    sid = src.id
    with _source_lock(session, sid, "a check is running for this source; try again when it finishes"):
        if session.scalar(select(Source.id).where(Source.id == sid)) is None:
            raise HTTPException(404, "custom source not found")  # deleted while we waited
        raw_files = [p for p in session.scalars(
            select(Run.raw_path).where(Run.source_id == sid, Run.raw_path.is_not(None)))]
        # Tables without ON DELETE CASCADE go by hand, children of runs first.
        for model in (Change, PendingChange, Upload, DataPoint, Indicator, ChangeRollup):
            session.execute(delete(model).where(model.source_id == sid))
        session.execute(delete(WatchTarget).where(WatchTarget.source_id == sid))  # snapshots cascade
        session.execute(delete(Run).where(Run.source_id == sid))
        session.execute(delete(Source).where(Source.id == sid))
        session.commit()
    raw_root = Path(get_settings().raw_dir).resolve()
    for f in raw_files:
        fp = Path(f).resolve()
        if fp.is_relative_to(raw_root):
            fp.unlink(missing_ok=True)
    d = (raw_root / slug).resolve()
    if d.parent == raw_root:  # a direct child of raw_dir only; never follow a slug out of it
        shutil.rmtree(d, ignore_errors=True)


@router.patch("/watch-targets/{target_id}")
def patch_target(target_id: int, body: TargetPatch, session: Session = Depends(get_session)):
    t = _target_or_404(session, target_id)
    new_name = body.kpi_name if body.kpi_name is not None else t.kpi_name
    new_url = body.url if body.url is not None else t.url
    renames, repoints = new_name != t.kpi_name, new_url != t.url
    new_key = body.credential_id if "credential_id" in body.model_fields_set else t.credential_id
    if body.use_browser is not None:
        wanted = body.use_browser
    elif new_key != t.credential_id and _is_login(session, t.credential_id):
        wanted = False  # browser mode came with the login being replaced
    else:
        wanted = t.use_browser
    use_browser = _check_key(session, new_key, new_url, wanted)
    disables = body.enabled is False and t.enabled
    # A key change sets needs_baseline; under the lock no run can be fetching with the old key
    # (and clear the flag when it finishes), and a held run diffed with the old key is dropped.
    rekeys = new_key != t.credential_id
    if not (renames or repoints or disables or rekeys):
        _apply_patch(session, t, body, new_name, new_url, use_browser)
        return _target_json(t, key_name=_key_name(session, t.credential_id))
    with _source_lock(session, t.source_id):
        session.refresh(t)  # state may have changed while waiting
        _apply_patch(session, t, body, new_name, new_url, use_browser, supersede=True)
    return _target_json(t, key_name=_key_name(session, t.credential_id))


# Held runs were diffed against the targets as they were; applying one after a KPI changed
# would write data under a stale name, link or key, so changing a KPI drops them.
HELD_REASON = "a change to the source's KPIs"


def _apply_patch(session: Session, t: WatchTarget, body: TargetPatch, new_name: str,
                 new_url: str, use_browser: bool, supersede: bool = False) -> None:
    if (new_name, new_url) != (t.kpi_name, t.url):
        msg = _conflict(session, t.source_id, new_name, new_url, exclude_id=t.id)
        if msg:
            raise HTTPException(409, msg)
    old_name = t.kpi_name
    try:
        if new_url != t.url:
            _delete_points(session, t.source_id, old_name)  # a new link is a new baseline
            t.url, t.needs_baseline = new_url, True
            t.last_status, t.last_checked_at, t.last_error = "never", None, None
        if new_name != old_name:
            # a stale indicator of a deleted KPI may hold the new code (no target uses it,
            # _conflict checked that)
            session.execute(delete(Indicator).where(
                Indicator.source_id == t.source_id, Indicator.code == new_name))
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
        if "kind" in body.model_fields_set and body.kind is not None:
            t.kind = body.kind
        t.use_browser = use_browser
        if "credential_id" in body.model_fields_set and body.credential_id != t.credential_id:
            # Like a re-point: with a different key (or none) the page may show different
            # content (a teaser vs the full page), so the next check is a silent baseline.
            t.credential_id, t.needs_baseline = body.credential_id, True
        if "selector" in body.model_fields_set:
            t.selector = body.selector or None
        if supersede:
            _supersede_held(session, t.source_id, reason=HELD_REASON)
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "conflicting KPI") from None


@router.delete("/watch-targets/{target_id}", status_code=204)
def delete_target(target_id: int, session: Session = Depends(get_session)):
    t = _target_or_404(session, target_id)
    with _source_lock(session, t.source_id):
        session.refresh(t)
        _delete_points(session, t.source_id, t.kpi_name)
        session.execute(delete(Indicator).where(
            Indicator.source_id == t.source_id, Indicator.code == t.kpi_name))
        session.delete(t)
        _supersede_held(session, t.source_id, reason=HELD_REASON)
        session.commit()


@router.post("/watch-targets/test")
def test_link(body: TestIn, session: Session = Depends(get_session)):
    """Fetch and extract once; nothing is written. With a login key the page is rendered
    with the saved session only: no sign-in is tried (status login_needed instead)."""
    use_browser = _check_key(session, body.credential_id, body.url, body.use_browser)
    entry = {"id": 0, "kpi_name": "", "url": body.url, "kind": body.kind,
             "selector": body.selector or None}
    cred = None
    state = None
    if body.credential_id is not None:
        try:
            cred = creds.load(session, body.credential_id)
        except creds.KeyUnavailable as e:
            return {"status": "error", "kind": body.kind, "error": f"key unavailable ({e})"}
        if cred.kind == "login":
            state = logins.load_state(session, cred)
        session.commit()  # end the read transaction: none held open during the fetch
    with httpx.Client(timeout=30) as client:
        try:
            if cred is not None and cred.kind == "login":
                res = preflight(client, FetchState(), body.url)
                if res is None:
                    res, _, _ = logins.browser_fetch(body.url, cred, state, body.kind)
            else:
                res = fetch_target(client, body.url, use_browser, credential=cred)
            e = _process(entry, res, cred)
            if e.get("error"):
                e["error"] = creds.redact(e["error"], cred)
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
    keys = {row.name.lower(): row for row in session.scalars(select(Credential))}
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
        key_name = (r.get("key_name") or "").strip()
        if key_name:
            if key_name.lower() not in keys:
                res.update(status="invalid", error=f"unknown key: {key_name}")
                out.append(res)
                continue
            if urlsplit(r["url"]).scheme != "https":
                res.update(status="invalid", error=NEEDS_HTTPS)
                out.append(res)
                continue
            key = keys[key_name.lower()]
            if key.kind == "login":
                err = creds.login_host_error(key, r["url"])
                if err:
                    res.update(status="invalid", error=err)
                    out.append(res)
                    continue
                res["use_browser"] = True  # a login KPI is always fetched in the browser
            res["credential_id"] = key.id
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
    try:
        return _confirm_rows(session, rows)
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "import conflicts with existing sources, try again") from None


def _confirm_rows(session: Session, rows: list[dict]) -> dict:
    created_sources = created_targets = skipped = 0
    known_keys = {row.id: row for row in session.scalars(select(Credential))}
    for r in rows:
        if r["status"] not in ("new_source", "existing_source"):
            skipped += 1
            continue
        cid = r.get("credential_id")
        if cid is not None and cid not in known_keys:
            skipped += 1  # key deleted since the preview
            continue
        # The key may have changed since the preview: decide browser mode and hosts now.
        use_browser = cid is not None and known_keys[cid].kind == "login"
        if use_browser and creds.login_host_error(known_keys[cid], r["url"]):
            skipped += 1
            continue
        source = _find_source(session, r["source_name"])
        if source is not None and _conflict(session, source.id, r["kpi_name"], r["url"]):
            skipped += 1  # created since the preview
            continue
        if source is None:
            source = _new_source(session, r["source_name"])
            created_sources += 1
        _add_target(session, source, r["kpi_name"], r["url"], use_browser=use_browser,
                    credential_id=cid)
        created_targets += 1
    session.commit()
    return {"created_sources": created_sources, "created_targets": created_targets,
            "skipped": skipped}
