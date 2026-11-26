"""Connector for DB-defined 'watch' sources (watched web pages / API links).

download() fetches every enabled target into ONE raw JSON file; parse() is pure over
that file, so approve/reprocess works from the stored raw file alone. after_run()
(called by the pipeline) writes snapshots and target status from the same file.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
from sqlalchemy import delete, select

from app import credentials as creds
from app.config import get_settings
from app.connectors.base import IndicatorMeta, ParseResult
from app.connectors.watch.extract import extract_page, flatten_json
from app.connectors.watch.fetch import FetchState, fetch_target
from app.core.canonical import RawRow
from app.models import WatchSnapshot, WatchTarget

log = logging.getLogger(__name__)

BINARY_TEXT = "binary content changed"
MIN_SNAPSHOTS = 10  # noisy detection looks at the last 10, so never keep fewer


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_binary(ctype: str) -> bool:
    c = ctype.lower()
    return bool(c) and not (c.startswith("text/") or "json" in c or "xml" in c or "html" in c)


def _decode(body: bytes, ctype: str) -> str:
    charset = "utf-8"
    for part in ctype.split(";")[1:]:
        k, _, v = part.strip().partition("=")
        if k.lower() == "charset" and v:
            charset = v.strip("\"'")
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _entry(t: dict, status: str, ctype: str = "", error: str | None = None, **extra) -> dict:
    return {"id": t["id"], "kpi_name": t["kpi_name"], "url": t["url"], "kind": t["kind"],
            "credential_id": t.get("credential_id"),
            "needs_baseline": t.get("needs_baseline", False), "status": status, "content_type": ctype, "error": error, **extra}


def _scrub(data: Any, cred: creds.Credential | None) -> Any:
    """Redact the secret from every string (keys too) of parsed JSON."""
    if cred is None:
        return data
    if isinstance(data, str):
        return creds.redact(data, cred)
    if isinstance(data, list):
        return [_scrub(v, cred) for v in data]
    if isinstance(data, dict):
        return {_scrub(k, cred): _scrub(v, cred) for k, v in data.items()}
    return data


def _process(t: dict, res, cred: creds.Credential | None = None) -> dict:
    if res.status != "ok":
        return _entry(t, res.status, res.content_type, res.error)
    ctype = res.content_type
    kind = t["kind"]
    if kind == "auto":
        kind = "api" if "json" in ctype.lower() else "page"
    if kind == "api":
        try:
            # fetch_target already scrubbed the body; scrub the decoded strings again in case
            # an escape the byte-level pass did not know (e.g. \uXXXX) spelled the secret.
            data = _scrub(json.loads(_decode(res.body, ctype)), cred)
            flatten_json(data)  # deep nesting would blow up later in parse
        except (ValueError, RecursionError):
            return _entry(t, "error", ctype, "invalid JSON")
        return _entry(t, "ok", ctype, kind="api", json=data)
    if _is_binary(ctype):
        return _entry(t, "ok", ctype, kind="page", binary=True, text=BINARY_TEXT,
                      sha256=_sha256(res.body))
    text, err = extract_page(_decode(res.body, ctype), t["selector"])
    if err:
        return _entry(t, "error", ctype, err)
    text = creds.redact(text, cred)  # HTML entities may have spelled the secret
    return _entry(t, "ok", ctype, kind="page", text=text, sha256=_sha256(text.encode("utf-8")))


def _api_text(data: Any) -> str:
    return "\n".join(f"{k}: {v}" for k, v in sorted(flatten_json(data).items()))


class WatchConnector:
    area = "custom"
    access = "watch"
    raw_suffix = ".json"

    def __init__(self, source_id: int, slug: str, name: str, session_factory: Callable[[], Any]):
        self.source_id = source_id
        self.slug = slug
        self.name = name
        self.session_factory = session_factory

    def download(self, client: httpx.Client, dest: Path) -> None:
        with self.session_factory() as session:  # short session, closed before any network I/O
            targets = [
                {"id": t.id, "kpi_name": t.kpi_name, "url": t.url, "kind": t.kind,
                 "selector": t.selector, "use_browser": t.use_browser,
                 "needs_baseline": t.needs_baseline, "credential_id": t.credential_id}
                for t in session.scalars(
                    select(WatchTarget)
                    .where(WatchTarget.source_id == self.source_id, WatchTarget.enabled.is_(True))
                    .order_by(WatchTarget.id)
                )
            ]
            # Decrypt each key here, before any network I/O; a key that cannot be read fails
            # only its own targets.
            keys: dict[int, creds.Credential | str] = {}
            for cid in {t["credential_id"] for t in targets if t["credential_id"] is not None}:
                try:
                    keys[cid] = creds.load(session, cid)
                except creds.KeyUnavailable as e:
                    keys[cid] = f"key unavailable ({e})"
                except KeyError:
                    keys[cid] = "key unavailable (key not found)"
        state = FetchState()
        results = []
        key_errors: dict[int, str | None] = {cid: None for cid in keys}
        for t in targets:
            cid = t["credential_id"]
            key = keys.get(cid) if cid is not None else None
            if isinstance(key, str):
                results.append(_entry(t, "error", error=key))
                key_errors[cid] = key
                continue
            try:
                res = fetch_target(client, t["url"], t["use_browser"], state, credential=key)
                if res.status == "key_rejected":
                    key_errors[cid] = res.error
                results.append(_process(t, res, key))
            except Exception as e:  # noqa: BLE001 - one bad target must not fail the download
                log.error("watch target %s failed unexpectedly: %s", t["id"], type(e).__name__)
                results.append(_entry(t, "error", error=f"unexpected error: {type(e).__name__}"))
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_text(json.dumps({"targets": results}), encoding="utf-8")
        tmp.replace(dest)
        if keys:  # after the run, in its own session: no row lock held on the key
            with self.session_factory() as session:
                for cid, err in key_errors.items():
                    try:
                        creds.mark_used(session, cid, err)
                    except Exception:  # noqa: BLE001 - bookkeeping must not fail the download
                        log.warning("could not record use of key %s", cid)

    def parse(self, raw: Path) -> ParseResult:
        targets = json.loads(Path(raw).read_text(encoding="utf-8"))["targets"]
        rows: list[RawRow] = []
        warnings: list[str] = []
        failed = 0
        for t in targets:
            name = t["kpi_name"]
            if t["status"] != "ok":
                failed += 1
                warnings.append(f"{name} ({t['status']}: {t.get('error')})")
                continue
            if "json" in t:
                for path, leaf in flatten_json(t["json"]).items():
                    rows.append(RawRow("api", name, "", "", leaf, {"path": path}))
            else:
                rows.append(RawRow("page", name, "", "", t["sha256"]))
        # Per-target errors in the raw file are already scrubbed of any key.
        shown = ", ".join(warnings[:5]) + (", …" if len(warnings) > 5 else "")
        if targets and failed == len(targets):
            raise RuntimeError(f"all targets failed: {shown}")
        if warnings:  # one summary line, so the pipeline's "incomplete:" handling sees it
            warnings = [f"incomplete: {len(warnings)} KPI(s) not checked: {shown}"]
        return ParseResult(
            rows=rows,
            complete=failed == 0,
            indicators=[IndicatorMeta(code=t["kpi_name"], name=t["kpi_name"]) for t in targets],
            vintage=None,
            warnings=warnings,
            baseline_indicators=[t["kpi_name"] for t in targets if t.get("needs_baseline")],
        )

    def on_failed(self, session, run) -> None:
        """A failed run (e.g. every target failed) still updates each target's status."""
        try:
            if not run.raw_path or not Path(run.raw_path).exists():
                return
            self._write(session, run, snapshots=False)
        except Exception:  # noqa: BLE001 - never raises
            log.exception("watch on_failed failed for run %s", getattr(run, "id", None))
            session.rollback()

    def after_run(self, session, run) -> None:
        """Write snapshots and target status for this run (idempotent per run)."""
        self._write(session, run, snapshots=True)

    def _write(self, session, run, snapshots: bool) -> None:
        targets = json.loads(Path(run.raw_path).read_text(encoding="utf-8"))["targets"]
        now = datetime.now(timezone.utc)
        keep = max(get_settings().raw_retention_runs, MIN_SNAPSHOTS)
        for t in targets:
            target = session.get(WatchTarget, t["id"])
            if target is None or target.source_id != self.source_id:
                continue
            target.last_status = t["status"]
            target.last_checked_at = now
            target.last_error = t.get("error")
            if snapshots and t["status"] == "ok":
                # A held (suspect_pending) run stored nothing, so the KPI is not baselined yet;
                # approve_run calls this hook again once the data is applied.
                # Not re-pointed and key not changed since the download.
                if (target.url == t["url"] and target.credential_id == t.get("credential_id")
                        and run.status in ("ok", "no_change", "baseline")):
                    target.needs_baseline = False
                self._snapshot(session, run, t)
                ids = session.scalars(
                    select(WatchSnapshot.id).where(WatchSnapshot.target_id == target.id)
                    .order_by(WatchSnapshot.id.desc()).offset(keep)
                ).all()
                if ids:
                    session.execute(delete(WatchSnapshot).where(WatchSnapshot.id.in_(ids)))
        session.commit()

    @staticmethod
    def _snapshot(session, run, t: dict) -> None:
        exists = session.scalar(
            select(WatchSnapshot.id).where(WatchSnapshot.target_id == t["id"],
                                           WatchSnapshot.run_id == run.id)
        )
        if exists:
            return
        if "json" in t:
            content = _api_text(t["json"])
            digest = _sha256(content.encode("utf-8"))
        else:
            content, digest = t["text"], t["sha256"]
        prev = session.scalar(
            select(WatchSnapshot.content_hash).where(WatchSnapshot.target_id == t["id"])
            .order_by(WatchSnapshot.id.desc()).limit(1)
        )
        session.add(WatchSnapshot(target_id=t["id"], run_id=run.id, content=content,
                                  content_hash=digest, changed=prev is not None and prev != digest))
