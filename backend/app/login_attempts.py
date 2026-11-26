"""Interactive sign-ins ("Sign in now"): the worker side of login_attempts.

The API creates a `pending` attempt; the worker claims it (`signing_in`), takes the login's
sign-in lock (waiting up to LOCK_WAIT_S for an automatic sign-in to finish) and holds it for the
whole attempt, code wait included, so scheduled runs see the login as busy and leave it alone.
The emailed code is typed in from the dashboard: the API stores it encrypted in
`login_attempts.code_enc`; the code provider takes it out (SELECT ... FOR UPDATE, decrypt, wipe,
status `verifying`) in one transaction. The window is CODE_WINDOW from entering `waiting_code`.

Every status write is compare-and-set: an attempt moves only from the statuses expected, and
the login session is written only while it is still exactly as this attempt last wrote it, so a
cancel, an expiry or a key edit (which resets the session and cancels the attempt) is never
overwritten. Transactions that touch both rows lock the session row first, then the attempt row
(the order the key-edit reset uses), so they cannot deadlock with it.

Nothing here logs or stores the password or the code: errors are redacted with both.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import replace
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app import credentials as creds
from app import logins
from app.connectors.watch.fetch import FetchState, preflight
from app.models import (
    ACTIVE_ATTEMPT_STATUSES,
    LoginAttempt,
    LoginSession,
    Run,
    Source,
    WatchTarget,
)

log = logging.getLogger(__name__)

CODE_WINDOW = timedelta(minutes=10)
STALE_AFTER = timedelta(minutes=15)
KEEP_ATTEMPTS = timedelta(days=30)  # finished attempts are pruned at startup after this
# Longer than one automatic sign-in can hold the lock (or a cancelled attempt finishing its
# current browser step), so "Sign in now" waits instead of failing with BUSY.
LOCK_WAIT_S = logins.SIGN_IN_BUDGET_S + logins.SUBMIT_WAIT_S + 15.0
LOCK_RETRY_S = 1.0
MAX_THREADS = 2
POLL_SECONDS = 2

EXPIRED = "this sign-in expired — click Sign in now again"
RESTARTED = "worker restarted — click Sign in now again"
BUSY = "another sign-in is running — try again in a minute"
CHANGED = "the login was changed while signing in — click Sign in now again"
CODE_TIMEOUT = "timed out waiting for the code"  # logins.sign_in's own timeout message
STUCK_LOGIN = ("signing_in", "waiting_code")  # login_sessions statuses of a sign-in in flight
# An interactive attempt that fails leaves the login login_needed; the user saw why in the
# dashboard, so it counts as alerted (a NULL notified_at there means "alert not delivered yet",
# see WatchConnector.notify_failed).
SEEN = {"notified_at": func.coalesce(LoginSession.notified_at, func.now())}


class _Gone(Exception):
    """The attempt was cancelled or expired while the browser waited for the code."""


def claim_pending(session_factory, limit: int) -> list[int]:
    """Move up to `limit` pending attempts (oldest first) to signing_in; returns their ids."""
    if limit <= 0:
        return []
    with session_factory() as s:
        ids = list(s.scalars(
            select(LoginAttempt.id).where(LoginAttempt.status == "pending")
            .order_by(LoginAttempt.id).limit(limit).with_for_update(skip_locked=True)))
        if ids:
            s.execute(update(LoginAttempt)
                      .where(LoginAttempt.id.in_(ids), LoginAttempt.status == "pending")
                      .values(status="signing_in"))
        s.commit()
    return ids


def queue_runs_for_login(session_factory, credential_id: int) -> list[int]:
    """Queue a manual run for every enabled source with enabled targets using this login,
    unless it already has a queued run. A running run may have fetched with the old session,
    so one run is queued behind it. Returns the new run ids."""
    out = []
    with session_factory() as s:
        sources = s.scalars(
            select(Source.id).join(WatchTarget, WatchTarget.source_id == Source.id)
            .where(WatchTarget.credential_id == credential_id, WatchTarget.enabled.is_(True),
                   Source.enabled.is_(True))
            .distinct().order_by(Source.id)).all()
        for sid in sources:
            queued = s.scalar(select(Run.id).where(Run.source_id == sid,
                                                   Run.status == "queued").limit(1))
            if queued is not None:
                continue
            run = Run(source_id=sid, status="queued", trigger="manual")
            try:
                with s.begin_nested():
                    s.add(run)
                    s.flush()
            except IntegrityError:  # uq_runs_one_queued_per_source: queued meanwhile
                continue
            out.append(run.id)
        s.commit()
    return out


def run_attempt(session_factory, attempt_id: int) -> None:
    """Run one claimed (signing_in) attempt to its end. Never raises."""
    a = _Attempt(session_factory, attempt_id)
    try:
        a.run()
    except Exception as e:  # noqa: BLE001 - never kill the worker; never log the message
        log.error("sign-in attempt %s crashed: %s", attempt_id, type(e).__name__)
        try:
            a.failed(f"login failed: {type(e).__name__}")
        except Exception:  # noqa: BLE001
            log.error("could not record the end of sign-in attempt %s", attempt_id)


class _Attempt:
    def __init__(self, session_factory, attempt_id: int) -> None:
        self.sf = session_factory
        self.id = attempt_id
        self.cid: int | None = None
        self.cred: creds.Credential | None = None
        self.code: str | None = None  # known only between the provider and the end
        self.version: tuple | None = None  # login session as this attempt last wrote it
        self.was: tuple[str, str | None] = ("login_needed", None)  # status, last_error before

    # --- helpers ------------------------------------------------------------------------

    def _redact(self, text: str) -> str:
        if self.cred is not None:
            text = creds.redact(text, self.cred)
            if self.code:
                text = creds.redact(text, replace(self.cred, kind="bearer", secret=self.code))
        return text[:500]

    @staticmethod
    def _ver(row) -> tuple:
        return (row.status, bytes(row.state_enc) if row.state_enc is not None else None,
                row.updated_at)

    def _lock_rows(self, s):
        """Lock the login session row, then the attempt row (see the module docstring)."""
        ls = s.execute(
            select(LoginSession.status, LoginSession.state_enc, LoginSession.updated_at,
                   LoginSession.last_error)
            .where(LoginSession.credential_id == self.cid).with_for_update()).first()
        att = s.execute(
            select(LoginAttempt.status, LoginAttempt.code_enc, LoginAttempt.waiting_since)
            .where(LoginAttempt.id == self.id).with_for_update()).first()
        return ls, att

    def _ours(self, ls) -> bool:
        return ls is not None and self.version is not None and self._ver(ls) == self.version

    def _write_ls(self, s, values: dict) -> None:
        new = s.execute(
            update(LoginSession).where(LoginSession.credential_id == self.cid)
            .values(**values, updated_at=func.clock_timestamp())
            .returning(LoginSession.status, LoginSession.state_enc, LoginSession.updated_at)
        ).one()
        self.version = self._ver(new)

    def _tx(self, decide: Callable) -> str | None:
        """One transaction: `decide(attempt_status, session_is_ours)` returns (attempt values,
        session values), either None. The session is written only while ours. Returns the
        attempt's status before."""
        with self.sf() as s:
            ls, att = self._lock_rows(s)
            st = att.status if att is not None else None
            a_vals, ls_vals = decide(st, self._ours(ls))
            if a_vals:
                s.execute(update(LoginAttempt).where(LoginAttempt.id == self.id).values(**a_vals))
            if ls_vals and self._ours(ls):
                self._write_ls(s, ls_vals)
            s.commit()
        return st

    def _status(self) -> str | None:
        with self.sf() as s:
            return s.scalar(select(LoginAttempt.status).where(LoginAttempt.id == self.id))

    def _end_attempt(self, status: str, error: str | None) -> None:
        """Before the session was touched: end the attempt if it is still active."""
        with self.sf() as s:
            s.execute(update(LoginAttempt)
                      .where(LoginAttempt.id == self.id,
                             LoginAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES))
                      .values(status=status, error=error, code_enc=None, finished_at=func.now()))
            s.commit()

    def _restore(self) -> dict:
        status, error = self.was
        return {"status": "login_needed" if status in STUCK_LOGIN else status,
                "last_error": error}

    # --- the attempt --------------------------------------------------------------------

    def run(self) -> None:
        with self.sf() as s:
            att = s.get(LoginAttempt, self.id)
            if att is None or att.status != "signing_in":
                return
            self.cid = att.credential_id
            bind = s.get_bind()
            try:
                self.cred = creds.load(s, self.cid)
            except KeyError:
                error = "key not found"
            except creds.KeyUnavailable as e:
                error = f"key unavailable ({e})"
            else:
                error = None if self.cred.kind == "login" else "this key is not a login"
        if error is not None:
            self._end_attempt("failed", error)
            return
        with ExitStack() as stack:
            if not self._acquire(stack, bind):
                return
            if not self._begin():
                return
            self._sign_in()

    def _acquire(self, stack: ExitStack, bind) -> bool:
        """Take the login's sign-in lock, waiting up to LOCK_WAIT_S for another sign-in."""
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                stack.enter_context(logins.login_lock(bind, self.cid))
                return True
            except logins.LoginBusy:
                pass
            if self._status() != "signing_in":  # cancelled meanwhile
                return False
            if time.monotonic() >= deadline:
                self._end_attempt("failed", BUSY)
                return False
            time.sleep(LOCK_RETRY_S)

    def _begin(self) -> bool:
        """Under the lock: the session goes to signing_in (if the attempt is still on)."""
        with self.sf() as s:
            ls, att = self._lock_rows(s)
            if att is None or att.status != "signing_in":
                s.rollback()
                return False
            if ls is None:  # every login key has one; be safe if it does not
                s.add(LoginSession(credential_id=self.cid, status="login_needed"))
                s.flush()
            else:
                self.was = (ls.status, ls.last_error)
            self._write_ls(s, {"status": "signing_in"})
            s.commit()
        return True

    def _provide(self) -> str | None:
        """The code provider handed to logins.sign_in (polled every 2 s)."""
        with self.sf() as s:
            ls, att = self._lock_rows(s)
            if att is None:
                s.rollback()
                raise _Gone()
            if att.status == "signing_in":  # first call: the site asked for the code
                s.execute(update(LoginAttempt).where(LoginAttempt.id == self.id)
                          .values(status="waiting_code", waiting_since=func.now()))
                if self._ours(ls):
                    self._write_ls(s, {"status": "waiting_code"})
                s.commit()
                return None
            if att.status != "waiting_code":
                s.rollback()
                raise _Gone()
            if att.code_enc is not None:  # typed in (within the window: the API checked)
                try:
                    code = creds.decrypt(att.code_enc)
                except creds.KeyUnavailable:
                    code = None
                s.execute(update(LoginAttempt).where(LoginAttempt.id == self.id).values(
                    code_enc=None, **({"status": "verifying"} if code else {})))
                if code and self._ours(ls):
                    self._write_ls(s, {"status": "signing_in"})
                s.commit()
                if not code:
                    raise RuntimeError("code unreadable")
                self.code = code
                return code
            now = s.scalar(select(func.now()))
            if now >= att.waiting_since + CODE_WINDOW:
                s.execute(update(LoginAttempt).where(LoginAttempt.id == self.id).values(
                    status="expired", error=EXPIRED, code_enc=None, finished_at=func.now()))
                s.commit()
                raise _Gone()
            s.rollback()
            return None

    def _sign_in(self) -> None:
        try:
            state = logins.sign_in(self.cred, self._provide,
                                   timeout_s=CODE_WINDOW.total_seconds() + 30)
        except (logins.LoginFailed, logins.LoginNeedsCode) as e:
            self.failed(str(e) or logins.LOGIN_NEEDED)
            return
        except Exception as e:  # noqa: BLE001 - playwright or anything else
            self.failed(f"login failed: {type(e).__name__}")
            return
        ok, state = self._verify(state)
        if not ok:
            self.failed(logins.SIGNED_IN_STILL_LOGIN)
            return
        data = creds.encrypt(json.dumps(
            logins.filter_state(state, creds.allowed_hosts(self.cred))))
        done = {"status": "succeeded", "error": None, "code_enc": None, "finished_at": func.now()}
        saved = {"state_enc": data, "status": "ok", "last_error": None, "notified_at": None}

        def decide(st, ours):
            if st in ("signing_in", "verifying") and ours:
                return done, saved
            if st in ACTIVE_ATTEMPT_STATUSES:  # the login changed under us: keep its state
                return {"status": "failed", "error": CHANGED, "code_enc": None,
                        "finished_at": func.now()}, None
            return None, self._after(st)

        if self._tx(decide) in ("signing_in", "verifying") and self.version[0] == "ok":
            self.code = None
            try:
                queue_runs_for_login(self.sf, self.cid)
            except Exception as e:  # noqa: BLE001 - the sign-in itself succeeded
                log.error("could not queue runs after sign-in %s: %s", self.id, type(e).__name__)

    def _verify(self, state: dict) -> tuple[bool, dict]:
        """One fetch of one KPI of this login with the new session. False when it still shows
        the login page; inconclusive results (no KPI, refused, errors) count as fine."""
        with self.sf() as s:
            rows = s.execute(
                select(WatchTarget.url, WatchTarget.kind).join(Source)
                .where(WatchTarget.credential_id == self.cid, WatchTarget.enabled.is_(True),
                       Source.enabled.is_(True))
                .order_by(WatchTarget.id)).all()
        target = next(((u, k) for u, k in rows if urlsplit(u).scheme == "https"
                       and creds.login_host_error(self.cred, u) is None), None)
        if target is None:
            return True, state
        url, kind = target
        try:
            with httpx.Client(timeout=30) as client:
                if preflight(client, FetchState(), url) is not None:
                    return True, state
            res, new_state, expired = logins.browser_fetch(url, self.cred, state, kind)
        except Exception as e:  # noqa: BLE001 - a check, not the sign-in
            log.warning("verification fetch after sign-in %s failed: %s", self.id,
                        type(e).__name__)
            return True, state
        if expired or res.status == "login_needed":
            return False, state
        return True, (new_state if res.status == "ok" and new_state is not None else state)

    def _after(self, st: str | None) -> dict:
        """The session after an attempt someone else ended (expired / cancelled)."""
        if st == "expired":
            return {"status": "login_needed", "last_error": EXPIRED, **SEEN}
        return self._restore()

    def failed(self, error: str) -> None:
        """End the attempt as failed (or expired, for the engine's own code timeout) unless
        it already ended; put the session back unless it changed under us."""
        if error == CODE_TIMEOUT:
            status, msg = "expired", EXPIRED
        else:
            status, msg = "failed", self._redact(error)

        def decide(st, ours):
            if st in ACTIVE_ATTEMPT_STATUSES:
                return ({"status": status, "error": msg, "code_enc": None,
                         "finished_at": func.now()},
                        {"status": "login_needed", "last_error": msg, **SEEN})
            return None, self._after(st)

        if self.cid is None:
            return
        st = self._tx(decide)
        self.code = None
        if st in ACTIVE_ATTEMPT_STATUSES and self.was[0] == "ok":
            # It was working: the stored session may still be fine. A run shows which, so
            # the badge corrects itself (R9 declined).
            try:
                queue_runs_for_login(self.sf, self.cid)
            except Exception as e:  # noqa: BLE001
                log.error("could not queue runs after failed sign-in %s: %s", self.id,
                          type(e).__name__)


def sweep_stale_attempts(session_factory) -> int:
    """Worker startup (Review Focus 4). Active attempts older than STALE_AFTER, or started
    ones (not pending) whose login lock is free (no live sign-in), become failed with
    RESTARTED and the code wiped. Login
    sessions stuck in signing_in / waiting_code with a free lock go back to login_needed
    (last_error kept). Returns how many attempts were swept."""
    with session_factory() as s:
        bind = s.get_bind()
        now = s.scalar(select(func.now()))
        rows = s.execute(
            select(LoginAttempt.id, LoginAttempt.credential_id, LoginAttempt.created_at,
                   LoginAttempt.status)
            .where(LoginAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES))
            .order_by(LoginAttempt.id)).all()
        stuck = list(s.scalars(select(LoginSession.credential_id)
                               .where(LoginSession.status.in_(STUCK_LOGIN))))
        s.rollback()
    swept = 0
    for aid, cid, created, status in rows:
        with ExitStack() as stack:
            if created >= now - STALE_AFTER:
                if status == "pending":  # never started: this worker will pick it up
                    continue
                try:
                    stack.enter_context(logins.login_lock(bind, cid))
                except logins.LoginBusy:
                    log.info("sign-in attempt %s not swept: a sign-in is running", aid)
                    continue
            with session_factory() as s:
                res = s.execute(
                    update(LoginAttempt)
                    .where(LoginAttempt.id == aid,
                           LoginAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES))
                    .values(status="failed", error=RESTARTED, code_enc=None,
                            finished_at=func.now()))
                s.commit()
                swept += res.rowcount
    for cid in stuck:
        try:
            with logins.login_lock(bind, cid), session_factory() as s:
                s.execute(update(LoginSession)
                          .where(LoginSession.credential_id == cid,
                                 LoginSession.status.in_(STUCK_LOGIN))
                          .values(status="login_needed", updated_at=func.clock_timestamp()))
                s.commit()
        except logins.LoginBusy:
            continue
    prune_old_attempts(session_factory)
    return swept


def prune_old_attempts(session_factory) -> int:
    """Delete finished attempts (no longer active) that ended more than KEEP_ATTEMPTS ago
    (created_at when finished_at was never set). Returns how many were deleted."""
    with session_factory() as s:
        res = s.execute(delete(LoginAttempt).where(
            LoginAttempt.status.not_in(ACTIVE_ATTEMPT_STATUSES),
            func.coalesce(LoginAttempt.finished_at, LoginAttempt.created_at)
            < func.now() - KEEP_ATTEMPTS))
        s.commit()
    if res.rowcount:
        log.info("pruned %d sign-in attempt(s) older than %d days", res.rowcount,
                 KEEP_ATTEMPTS.days)
    return res.rowcount


class AttemptWorker:
    """Polls for pending attempts (every POLL_SECONDS from the scheduler) and runs each in its
    own thread, at most `max_threads` at a time. Each thread opens its own sessions."""

    def __init__(self, session_factory, max_threads: int = MAX_THREADS,
                 run: Callable = run_attempt) -> None:
        self.sf = session_factory
        self.max_threads = max_threads
        self._run_fn = run
        self._mu = threading.Lock()
        self._threads: dict[int, threading.Thread] = {}

    def poll(self) -> int:
        """Start threads for pending attempts while slots are free; returns how many."""
        with self._mu:
            free = self.max_threads - len(self._threads)
        if free <= 0:
            return 0
        ids = claim_pending(self.sf, free)
        for aid in ids:
            t = threading.Thread(target=self._run, args=(aid,), name=f"sign-in-{aid}",
                                 daemon=True)
            with self._mu:
                self._threads[aid] = t
            t.start()
        return len(ids)

    def _run(self, aid: int) -> None:
        try:
            self._run_fn(self.sf, aid)
        except Exception as e:  # noqa: BLE001
            log.error("sign-in attempt %s crashed: %s", aid, type(e).__name__)
        finally:
            with self._mu:
                self._threads.pop(aid, None)

    def join(self, timeout: float | None = None) -> None:
        with self._mu:
            threads = list(self._threads.values())
        for t in threads:
            t.join(timeout)
