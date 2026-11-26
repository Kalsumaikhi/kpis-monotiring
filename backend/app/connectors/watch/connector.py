"""Connector for DB-defined 'watch' sources (watched web pages / API links).

download() fetches every enabled target into ONE raw JSON file; parse() is pure over
that file, so approve/reprocess works from the stored raw file alone. after_run()
(called by the pipeline) writes snapshots and target status from the same file.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, func, select, update

from app import credentials as creds
from app import logins
from app.config import get_settings
from app.connectors.base import IndicatorMeta, ParseResult
from app.connectors.watch.extract import extract_page, flatten_json
from app.connectors.watch.fetch import NEEDS_HTTPS, FetchState, fetch_target, preflight
from app.core.canonical import RawRow
from app.models import LoginSession, WatchSnapshot, WatchTarget

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


# A sign-in started from the dashboard (Task 4) is running: leave the session alone.
INTERACTIVE = ("signing_in", "waiting_code", "verifying")


@dataclass
class _Login:
    """One login's state during a download. `version` is (status, state_enc, updated_at) as
    last read or written; every write is compare-and-set against it, so a session that was
    reset or re-signed elsewhere since is never overwritten."""
    cred: creds.Credential
    state: dict | None
    version: tuple
    auto_ok: bool = False  # an automatic sign-in may be tried (see _auto_ok)
    skip: bool = False  # no more browser work and no session writes this run
    tried: bool = False  # automatic sign-in already tried this run
    dead: str | None = None  # sign-in failed: no further browser work this run
    fetched_ok: bool = False
    signed_in: bool = False  # an automatic sign-in succeeded this run
    still_login: bool = False  # ... yet a page fetched after it still showed the login form
    realert: bool = False  # login_needed after a failure, but the alert was never delivered


def _api_text(data: Any) -> str:
    return "\n".join(f"{k}: {v}" for k, v in sorted(flatten_json(data).items()))


def _only_login_needed(targets: list[dict]) -> bool:
    """True when some KPIs were not checked and every one of them waits for a sign-in."""
    unchecked = [t for t in targets if t["status"] != "ok"]
    return bool(unchecked) and all(t["status"] == "login_needed" for t in unchecked)


def _run_only_login_needed(run) -> bool:
    try:
        if not run.raw_path or not Path(run.raw_path).exists():
            return False
        return _only_login_needed(
            json.loads(Path(run.raw_path).read_text(encoding="utf-8"))["targets"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


class WatchConnector:
    area = "custom"
    access = "watch"
    raw_suffix = ".json"

    def __init__(self, source_id: int, slug: str, name: str, session_factory: Callable[[], Any]):
        self.source_id = source_id
        self.slug = slug
        self.name = name
        self.session_factory = session_factory
        self._alerts: list[dict] = []  # logins this run moved into login_needed (see notify_extras)
        self._handed: list[dict] = []  # alerts handed to the notifier (see notify_failed)

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
            # Logins: the saved session is read once per login, before any network I/O.
            sessions: dict[int, _Login] = {}
            for cid, key in keys.items():
                if isinstance(key, creds.Credential) and key.kind == "login":
                    sessions[cid] = self._load_login(session, key)
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
                if cid in sessions:
                    results.append(self._login_target(client, state, t, sessions[cid]))
                    continue
                res = fetch_target(client, t["url"], t["use_browser"], state, credential=key)
                if res.status == "key_rejected":
                    key_errors[cid] = res.error
                results.append(_process(t, res, key))
            except Exception as e:  # noqa: BLE001 - one bad target must not fail the download
                log.error("watch target %s failed unexpectedly: %s", t["id"], type(e).__name__)
                results.append(_entry(t, "error", error=f"unexpected error: {type(e).__name__}"))
        for cid, lg in sessions.items():
            try:
                self._finish_login(lg)
            except Exception:  # noqa: BLE001 - bookkeeping must not fail the download
                log.warning("could not save the session of login %s", cid)
            if lg.skip:
                key_errors.pop(cid, None)  # nothing was tried with it
            else:
                key_errors[cid] = lg.dead
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_text(json.dumps({"targets": results}), encoding="utf-8")
        tmp.replace(dest)
        if key_errors:  # after the run, in its own session: no row lock held on the key
            with self.session_factory() as session:
                for cid, err in key_errors.items():
                    try:
                        creds.mark_used(session, cid, err)
                    except Exception:  # noqa: BLE001 - bookkeeping must not fail the download
                        log.warning("could not record use of key %s", cid)

    # --- logins ---------------------------------------------------------------------------

    def notify_extras(self, session, run) -> dict:
        """For the pipeline's notifier: the logins this run moved into login_needed (and
        claimed the alert for). Handed out once."""
        alerts, self._alerts = self._alerts, []
        self._handed = alerts
        out: dict = {"login_needed": alerts} if alerts else {}
        if _run_only_login_needed(run):
            out["incomplete_note"] = None  # the login line says it; no "incomplete fetch" too
        return out

    def notify_failed(self, session, run) -> None:
        """The run's message was not delivered: un-claim its login-needed alerts, so the next
        run that finds those logins still needing a sign-in alerts again."""
        alerts, self._alerts, self._handed = self._alerts + self._handed, [], []
        ids = {a["credential_id"] for a in alerts}
        if not ids:
            return
        session.execute(
            update(LoginSession)
            .where(LoginSession.credential_id.in_(ids), LoginSession.status == "login_needed")
            .values(notified_at=None, updated_at=LoginSession.updated_at))
        session.commit()

    def _alert(self, cred: creds.Credential) -> None:
        if not any(a["credential_id"] == cred.id for a in self._alerts):
            self._alerts.append({"login_name": cred.name, "source": self.name,
                                 "credential_id": cred.id})

    @staticmethod
    def _auto_ok(row) -> bool:
        """Automatic sign-in only for a working login whose session expired, or a fresh or
        reset one never tried (login_needed, no error). Never again after it needed a code or
        failed: no code email every run, no lockout from repeated bad passwords. The user
        recovers with "Sign in now" or by editing the key (which resets it)."""
        return row.status == "ok" or (row.status == "login_needed" and row.last_error is None)

    @staticmethod
    def _realert(row) -> bool:
        """Needs the user (no automatic sign-in) but no alert went out: the last one could
        not be delivered (notify_failed)."""
        return (row.status == "login_needed" and row.last_error is not None
                and row.notified_at is None)

    @staticmethod
    def _version(row) -> tuple:
        return (row.status, bytes(row.state_enc) if row.state_enc is not None else None,
                row.updated_at)

    def _load_login(self, session, cred: creds.Credential) -> _Login:
        row = session.get(LoginSession, cred.id)
        if row is None:  # every login key has one; be safe if it does not
            row = LoginSession(credential_id=cred.id, status="login_needed")
            session.add(row)
            session.commit()
            session.refresh(row)
        if row.status in INTERACTIVE:
            return _Login(cred, None, self._version(row), skip=True)
        return _Login(cred, logins.load_state(session, cred), self._version(row),
                      auto_ok=self._auto_ok(row), realert=self._realert(row))

    def _reload(self, lg: _Login) -> None:
        """The session changed under us (reset, or saved by another run): take the stored one."""
        with self.session_factory() as session:
            row = session.get(LoginSession, lg.cred.id)
            if row is None or row.status in INTERACTIVE:
                lg.skip = True
                return
            lg.version = self._version(row)
            lg.auto_ok = self._auto_ok(row)
            lg.realert = self._realert(row)
            lg.state = logins.load_state(session, lg.cred)

    def _cas(self, lg: _Login, values: dict, into_login_needed: bool = False,
             was: str | None = None) -> bool | None:
        """Write `values` if the session is still as last seen. Returns None when it changed
        (nothing written); else whether this write claimed the login-needed alert. `was` is
        the status before our own 'signing_in' (else the stored status is used)."""
        status, state_enc, updated_at = lg.version
        with self.session_factory() as session:
            cid = LoginSession.credential_id == lg.cred.id
            row = session.execute(
                select(LoginSession.status, LoginSession.notified_at)
                .where(cid, LoginSession.status == status, LoginSession.updated_at == updated_at,
                       LoginSession.state_enc.is_not_distinct_from(state_enc))
                .with_for_update()
            ).first()
            if row is None:
                session.rollback()
                return None
            values = dict(values)
            # Alert once per transition: never alerted yet, or it was ok since the last alert.
            alert = into_login_needed and (row.notified_at is None
                                           or (was or row.status) == "ok")
            if alert:
                values["notified_at"] = func.now()
            new = session.execute(
                update(LoginSession).where(cid)
                .values(**values, updated_at=func.clock_timestamp())
                .returning(LoginSession.status, LoginSession.state_enc, LoginSession.updated_at)
            ).one()
            session.commit()
        lg.version = self._version(new)
        return alert

    def _save_ok(self, lg: _Login) -> None:
        data = json.dumps(logins.filter_state(lg.state or {}, creds.allowed_hosts(lg.cred)))
        written = self._cas(lg, {"state_enc": creds.encrypt(data), "status": "ok",
                                 "last_error": None, "notified_at": None})
        if written is None:
            self._reload(lg)

    def _mark_login_needed(self, lg: _Login, error: str, was: str | None = None) -> None:
        alert = self._cas(lg, {"status": "login_needed", "last_error": error},
                          into_login_needed=True, was=was)
        if alert:
            self._alert(lg.cred)

    def _bind(self):
        with self.session_factory() as session:
            return session.get_bind()

    def _sign_in(self, lg: _Login, state: FetchState) -> bool:
        """One automatic sign-in (no code), under the per-login lock and with the status
        'signing_in' meanwhile. False when it did not succeed: failed (the login is marked
        login_needed) or another sign-in is running (the session is left alone); either way
        the login gets no more browser work this run."""
        cred = lg.cred
        try:
            with logins.login_lock(self._bind(), cred.id):
                was = lg.version[0]
                if self._cas(lg, {"status": "signing_in"}) is None:
                    lg.skip = True  # changed since we read it: leave it to whoever did
                    return False
                try:
                    return self._signing_in(lg, state, was)
                finally:
                    if lg.version[0] == "signing_in":  # nothing recorded an outcome
                        self._cas(lg, {"status": was})
        except logins.LoginBusy:
            lg.skip = True
            return False

    def _signing_in(self, lg: _Login, state: FetchState, was: str) -> bool:
        cred = lg.cred
        state.throttle(urlsplit(cred.login_url or "").netloc)
        try:
            new_state = logins.sign_in(cred, None)
        except (logins.LoginNeedsCode, logins.LoginFailed) as e:
            error = str(e) or logins.LOGIN_NEEDED
        except Exception as e:  # noqa: BLE001 - one login must not fail the download
            error = f"login failed: {type(e).__name__}"
        else:
            lg.state = new_state
            self._save_ok(lg)
            lg.signed_in = True
            return True
        lg.dead = creds.redact(error, cred)[:500]
        self._mark_login_needed(lg, lg.dead, was=was)
        return False

    def _login_target(self, client: httpx.Client, state: FetchState, t: dict,
                      lg: _Login) -> dict:
        cred = lg.cred
        if lg.skip or lg.dead:
            return _entry(t, "login_needed", error=logins.LOGIN_NEEDED)
        url = t["url"]
        if urlsplit(url).scheme != "https":
            return _entry(t, "error", error=NEEDS_HTTPS)
        wrong_host = creds.login_host_error(cred, url)
        if wrong_host:  # e.g. the host was removed from "Also used on" since
            return _entry(t, "error", error=wrong_host)
        refused = preflight(client, state, url)
        if refused is not None:
            return _process(t, refused, cred)
        res, new_state, expired = logins.browser_fetch(url, cred, lg.state, t["kind"])
        if expired and not lg.tried:
            lg.tried = True
            if not lg.auto_ok:  # already needs the user: no automatic retry (R-a)
                if lg.realert:  # ... and was never told: alert now (once)
                    lg.realert = False
                    if self._cas(lg, {}, into_login_needed=True):
                        self._alert(cred)
                lg.skip = True
                return _entry(t, "login_needed", error=logins.LOGIN_NEEDED)
            if not self._sign_in(lg, state):
                return _entry(t, "login_needed", error=logins.LOGIN_NEEDED)
            if lg.skip:  # the session was taken over while signing in
                return _entry(t, "login_needed", error=logins.LOGIN_NEEDED)
            state.throttle(urlsplit(url).netloc)
            res, new_state, expired = logins.browser_fetch(url, cred, lg.state, t["kind"])
        if expired or res.status == "login_needed":
            # Never the login page as content. After a fresh sign-in this KPI is marked; if
            # no KPI of the login fetches ok, _finish_login marks the login itself.
            lg.still_login = lg.still_login or lg.signed_in
            return _entry(t, "login_needed", error=logins.LOGIN_NEEDED)
        if res.status == "ok":
            lg.state, lg.fetched_ok = new_state, True
        entry = _process(t, res, cred)
        if entry.get("error"):
            entry["error"] = creds.redact(entry["error"], cred)
        return entry

    def _finish_login(self, lg: _Login) -> None:
        """Save the refreshed session (cookies may rotate) after successful fetches. A sign-in
        that 'worked' while every page still showed the login form marks the login
        login_needed: no automatic retry every run (R-a), and one alert."""
        if lg.skip or lg.dead:
            return
        if lg.signed_in and lg.still_login and not lg.fetched_ok:
            lg.dead = logins.SIGNED_IN_STILL_LOGIN
            self._mark_login_needed(lg, lg.dead)
            return
        if lg.fetched_ok:
            self._save_ok(lg)

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
        waiting = _only_login_needed(targets)
        if targets and failed == len(targets) and not waiting:
            raise RuntimeError(f"all targets failed: {shown}")
        # Every KPI waiting for a sign-in is an incomplete run, not a failed one (R10): no
        # "check failed" or failure streak; the login's own message says what to do.
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
