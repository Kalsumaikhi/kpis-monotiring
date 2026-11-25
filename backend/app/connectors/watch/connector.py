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
            "status": status, "content_type": ctype, "error": error, **extra}


def _process(t: dict, res) -> dict:
    if res.status != "ok":
        return _entry(t, res.status, res.content_type, res.error)
    ctype = res.content_type
    kind = t["kind"]
    if kind == "auto":
        kind = "api" if "json" in ctype.lower() else "page"
    if kind == "api":
        try:
            data = json.loads(_decode(res.body, ctype))
        except ValueError:
            return _entry(t, "error", ctype, "invalid JSON")
        return _entry(t, "ok", ctype, kind="api", json=data)
    if _is_binary(ctype):
        return _entry(t, "ok", ctype, kind="page", binary=True, text=BINARY_TEXT,
                      sha256=_sha256(res.body))
    text, err = extract_page(_decode(res.body, ctype), t["selector"])
    if err:
        return _entry(t, "error", ctype, err)
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
                 "selector": t.selector, "use_browser": t.use_browser}
                for t in session.scalars(
                    select(WatchTarget)
                    .where(WatchTarget.source_id == self.source_id, WatchTarget.enabled.is_(True))
                    .order_by(WatchTarget.id)
                )
            ]
        state = FetchState()
        results = [
            _process(t, fetch_target(client, t["url"], t["use_browser"], state)) for t in targets
        ]
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_text(json.dumps({"targets": results}), encoding="utf-8")
        tmp.replace(dest)

    def parse(self, raw: Path) -> ParseResult:
        targets = json.loads(Path(raw).read_text(encoding="utf-8"))["targets"]
        rows: list[RawRow] = []
        warnings: list[str] = []
        failed = 0
        for t in targets:
            name = t["kpi_name"]
            if t["status"] != "ok":
                failed += 1
                warnings.append(f"{name}: {t['status']} ({t.get('error')})")
                continue
            if "json" in t:
                for path, leaf in flatten_json(t["json"]).items():
                    rows.append(RawRow("api", name, "", "", leaf, {"path": path}))
            else:
                rows.append(RawRow("page", name, "", "", t["sha256"]))
        if targets and failed == len(targets):
            raise RuntimeError("all targets failed")
        return ParseResult(
            rows=rows,
            complete=failed == 0,
            indicators=[IndicatorMeta(code=t["kpi_name"], name=t["kpi_name"]) for t in targets],
            vintage=None,
            warnings=warnings,
        )

    def after_run(self, session, run) -> None:
        """Write snapshots and target status for this run (idempotent per run)."""
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
            if t["status"] == "ok":
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
