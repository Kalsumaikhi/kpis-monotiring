"""Interactive "Sign in now" with an emailed one-time code (Logins plan Task 4).

The state-machine tests drive the worker functions directly in the test thread, with
`logins.sign_in` / `logins.browser_fetch` faked; the fake calls the code provider and lets the
test submit the code through the API between calls. `test_real_browser_code_flow` runs the real
engine and a real worker thread against tests/login_site.py (skips without Chromium).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
from sqlalchemy.orm import sessionmaker

from app import credentials as creds
from app import login_attempts as la
from app import logins
from app.api import deps
from app.api.main import create_app
from app.config import get_settings
from app.connectors.watch import fetch as fetch_mod
from app.connectors.watch.fetch import FetchResult
from app.core import pipeline
from app.models import Credential, LoginAttempt, LoginSession, Run, Source, WatchTarget

AUTH = ("anyone", "secret")
USER = "me@example.com"
PASSWORD = "Sup3r-secret-pass!"
LOGIN_URL = "https://portal.example/login"
CODE = "Q7-493105"
EXPIRED = "this sign-in expired — click Sign in now again"
RESTARTED = "worker restarted — click Sign in now again"
BUSY = "another sign-in is running — try again in a minute"
STILL_FORM = "signed in, but the pages still show the login form"
BAD_CODE = "the code can only contain letters, digits and dashes"
IN_PROGRESS = "a sign-in is already in progress"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("KPI_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(fetch_mod, "MIN_INTERVAL", 0.0)
    monkeypatch.setattr(la, "LOCK_RETRY_S", 0.05)
    real = fetch_mod.socket.getaddrinfo

    def fake(host, *a, **k):
        if host in ("localhost", "127.0.0.1"):
            return real(host, *a, **k)
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(fetch_mod.socket, "getaddrinfo", fake)


def make_client(get_session, factory, monkeypatch) -> TestClient:
    monkeypatch.setattr(get_settings(), "dashboard_password", "secret")
    app = create_app()
    app.dependency_overrides[deps.get_session] = get_session
    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    c = TestClient(app)
    c.auth = AUTH
    return c


@pytest.fixture
def client(db_session, session_factory, monkeypatch):
    def get_session():
        # One session per request, as in production: end its (read) transaction afterwards,
        # so worker writes made between requests are not nested inside it on the shared
        # test connection.
        yield db_session
        db_session.rollback()

    return make_client(get_session, session_factory, monkeypatch)


def mk_login(db_session, name="Portal", status="login_needed", last_error=None,
             login_url=LOGIN_URL, notified=False):
    row = Credential(name=name, kind="login", username=USER, secret_enc=creds.encrypt(PASSWORD),
                     login_url=login_url)
    db_session.add(row)
    db_session.flush()
    db_session.add(LoginSession(credential_id=row.id, status=status, last_error=last_error,
                                notified_at=datetime.now(timezone.utc) if notified else None))
    db_session.commit()
    return row


def mk_source(db_session, slug, enabled=True):
    src = Source(slug=slug, name=slug.title(), area="custom", access="watch", enabled=enabled)
    db_session.add(src)
    db_session.commit()
    return src


def add_target(db_session, src, cred, url="https://portal.example/a", name="A", enabled=True):
    t = WatchTarget(source_id=src.id, kpi_name=name, url=url, kind="page", use_browser=True,
                    credential_id=cred.id if cred else None, enabled=enabled)
    db_session.add(t)
    db_session.commit()
    return t


def sid_state(sid: str) -> dict:
    return {"cookies": [{"name": "sid", "value": sid, "domain": "portal.example", "path": "/",
                         "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"}],
            "origins": []}


def robots():
    respx.get(url__regex=r"https://[a-z.]+/robots\.txt").mock(
        return_value=httpx.Response(404))


class FakeSignIn:
    """Stands in for logins.sign_in: calls the code provider like the real engine does."""

    def __init__(self, monkeypatch, needs_code=True, accept=CODE):
        self.needs_code = needs_code
        self.accept = accept
        self.calls = 0
        self.on_start = None  # called before anything (status checks)
        self.on_wait = None  # called after the first provider call (the test submits a code)
        self.polls = 3
        self.fail_with = None
        monkeypatch.setattr(logins, "sign_in", self)

    def __call__(self, cred, code_provider, timeout_s=600):
        assert code_provider is not None
        self.calls += 1
        if self.on_start:
            self.on_start()
        if self.fail_with:
            raise logins.LoginFailed(self.fail_with)
        if self.needs_code:
            try:
                assert code_provider() is None
                if self.on_wait:
                    self.on_wait()
                code = None
                for _ in range(self.polls):
                    code = code_provider()
                    if code:
                        break
            except Exception:  # noqa: BLE001 - as the real engine
                raise logins.LoginFailed("could not read the code") from None
            if not code:
                raise logins.LoginFailed("timed out waiting for the code")
            if code != self.accept:
                # The real engine redacts; this fake does not, to prove the worker does.
                raise logins.LoginFailed(f"code was not accepted ({code}, {cred.secret})")
        return sid_state("new1")


class FakeFetch:
    def __init__(self, monkeypatch, expired=False):
        self.expired = expired
        self.urls: list[str] = []
        monkeypatch.setattr(logins, "browser_fetch", self)

    def __call__(self, url, cred, state, kind="page"):
        self.urls.append(url)
        if self.expired:
            return FetchResult("login_needed", error=logins.LOGIN_NEEDED), state, True
        rotated = json.loads(json.dumps(state))
        rotated["cookies"].append({"name": "rot", "value": "r1", "domain": "portal.example",
                                   "path": "/"})
        return FetchResult("ok", "text/html", b"<main>ok</main>"), rotated, False


def start(client, cid) -> int:
    r = client.post(f"/api/logins/{cid}/sign-in")
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "pending"
    return r.json()["attempt_id"]


def work(sf) -> list[int]:
    """One poll of the worker, run inline: claim pending attempts and run each."""
    ids = la.claim_pending(sf, 2)
    for aid in ids:
        la.run_attempt(sf, aid)
    return ids


def _detached(db_session, objs):
    """Read, detach, end the read transaction: on the shared test connection an open one
    would enclose the worker's later writes (and a later rollback would undo them)."""
    for o in objs:
        db_session.expunge(o)
    db_session.rollback()
    return objs


def attempt(db_session, aid) -> LoginAttempt:
    db_session.expire_all()
    return _detached(db_session, [db_session.get(LoginAttempt, aid)])[0]


def ls(db_session, cid) -> LoginSession:
    db_session.expire_all()
    return _detached(db_session, [db_session.get(LoginSession, cid)])[0]


def queued(db_session, src) -> list[Run]:
    db_session.expire_all()
    return _detached(db_session, list(db_session.scalars(
        select(Run).where(Run.source_id == src.id, Run.status == "queued"))))


def login_status(client, cid) -> str:
    return next(k for k in client.get("/api/credentials").json() if k["id"] == cid)["login_status"]


# --- API --------------------------------------------------------------------------------------

def test_start_attempt_202_and_409_when_active(client, db_session, monkeypatch):
    c = mk_login(db_session)
    aid = start(client, c.id)
    r = client.post(f"/api/logins/{c.id}/sign-in")
    assert r.status_code == 409 and r.json() == {"detail": IN_PROGRESS}
    r = client.get(f"/api/logins/{c.id}/attempt")
    assert r.status_code == 200
    assert r.json() == {"attempt_id": aid, "status": "pending", "error": None,
                        "waiting_since": None, "expires_at": None}
    assert client.post("/api/logins/999999/sign-in").status_code == 404
    assert client.get("/api/logins/999999/attempt").status_code == 404
    hdr = Credential(name="Hdr", kind="header", header_name="X-Key",
                     secret_enc=creds.encrypt("header-secret-123"))
    db_session.add(hdr)
    db_session.commit()
    assert client.post(f"/api/logins/{hdr.id}/sign-in").status_code == 422
    other = mk_login(db_session, name="Other")
    assert client.get(f"/api/logins/{other.id}/attempt").status_code == 404  # none yet
    # Once finished, a new one can start.
    assert client.post(f"/api/logins/attempts/{aid}/cancel").json()["status"] == "cancelled"
    assert start(client, c.id) != aid
    # No KPI_SECRET_KEY: nothing can be stored, so nothing starts.
    monkeypatch.setenv("KPI_SECRET_KEY", "")
    r = client.post(f"/api/logins/{other.id}/sign-in")
    assert r.status_code == 503


@respx.mock
def test_attempt_flow_waiting_code_then_success_queues_runs(client, db_session,
                                                           session_factory, monkeypatch):
    robots()
    c = mk_login(db_session, status="login_needed", last_error="the site asks for the emailed code",
                 notified=True)
    s1, s2, s3, s4 = (mk_source(db_session, f"custom-{n}") for n in ("one", "two", "three", "four"))
    add_target(db_session, s1, c, "https://portal.example/a", "A")
    add_target(db_session, s1, c, "https://portal.example/b", "B")
    add_target(db_session, s2, c, "https://portal.example/c", "C")
    s3.enabled = False
    db_session.commit()
    add_target(db_session, s3, c, "https://portal.example/d", "D")
    add_target(db_session, s4, c, "https://portal.example/e", "E", enabled=False)
    open_src = mk_source(db_session, "custom-open")
    add_target(db_session, open_src, None, "https://open.example/x", "X")
    db_session.add(Run(source_id=s2.id, status="queued", trigger="schedule"))
    db_session.commit()
    fake, fetch = FakeSignIn(monkeypatch), FakeFetch(monkeypatch)
    aid = start(client, c.id)
    seen = {}

    def on_start():
        seen["attempt"] = attempt(db_session, aid).status
        seen["login"] = login_status(client, c.id)

    def on_wait():
        r = client.get(f"/api/logins/{c.id}/attempt").json()
        seen["waiting"] = r
        seen["login_waiting"] = login_status(client, c.id)
        r = client.post(f"/api/logins/attempts/{aid}/code", json={"code": CODE})
        assert r.status_code == 202 and r.json() == {"attempt_id": aid, "status": "waiting_code"}
        stored = attempt(db_session, aid).code_enc
        assert stored is not None and CODE.encode() not in bytes(stored)
        assert creds.decrypt(stored) == CODE

    fake.on_start, fake.on_wait = on_start, on_wait
    assert work(session_factory) == [aid]
    assert seen["attempt"] == "signing_in" and seen["login"] == "signing_in"
    w = seen["waiting"]
    assert w["status"] == "waiting_code" and w["error"] is None
    since = datetime.fromisoformat(w["waiting_since"])
    assert datetime.fromisoformat(w["expires_at"]) - since == timedelta(minutes=10)
    assert seen["login_waiting"] == "waiting_code"
    a = attempt(db_session, aid)
    assert a.status == "succeeded" and a.code_enc is None and a.finished_at is not None
    row = ls(db_session, c.id)
    assert row.status == "ok" and row.last_error is None and row.notified_at is None
    state = json.loads(creds.decrypt(row.state_enc))
    assert {k["value"] for k in state["cookies"]} == {"new1", "r1"}  # verified + rotated
    assert fetch.urls == ["https://portal.example/a"]  # one verification fetch
    assert login_status(client, c.id) == "ok"
    assert client.get(f"/api/logins/{c.id}/attempt").json()["status"] == "succeeded"
    # One manual run per enabled source with enabled targets of this login, not if busy.
    assert [(r.trigger) for r in queued(db_session, s1)] == ["manual"]
    assert [(r.trigger) for r in queued(db_session, s2)] == ["schedule"]
    assert queued(db_session, s3) == [] and queued(db_session, s4) == []
    assert queued(db_session, open_src) == []
    assert work(session_factory) == []  # nothing left


def test_login_without_code_succeeds_and_no_target_no_fetch(client, db_session,
                                                            session_factory, monkeypatch):
    c = mk_login(db_session, status="ok")
    FakeSignIn(monkeypatch, needs_code=False)
    fetch = FakeFetch(monkeypatch)
    aid = start(client, c.id)
    work(session_factory)
    assert attempt(db_session, aid).status == "succeeded"
    assert ls(db_session, c.id).status == "ok" and fetch.urls == []


def test_code_after_expiry_410(client, db_session, session_factory, monkeypatch):
    c = mk_login(db_session)
    aid = start(client, c.id)
    # Bad codes are refused before anything else, and never echoed.
    for bad in ("12 34", "", "x" * 65, "abc$", 123456):
        r = client.post(f"/api/logins/attempts/{aid}/code", json={"code": bad})
        assert r.status_code == 422 and r.json() == {"detail": BAD_CODE}
    # Not waiting for a code yet.
    r = client.post(f"/api/logins/attempts/{aid}/code", json={"code": CODE})
    assert r.status_code == 409 and CODE not in r.text
    assert client.post("/api/logins/attempts/999999/code", json={"code": CODE}).status_code == 404
    # Waiting for 11 minutes: the window is over.
    db_session.execute(update(LoginAttempt).where(LoginAttempt.id == aid).values(
        status="waiting_code", waiting_since=datetime.now(timezone.utc) - timedelta(minutes=11)))
    db_session.commit()
    r = client.post(f"/api/logins/attempts/{aid}/code", json={"code": CODE})
    assert r.status_code == 410 and r.json() == {"detail": EXPIRED}
    a = attempt(db_session, aid)
    assert a.status == "expired" and a.code_enc is None and a.error == EXPIRED
    r = client.post(f"/api/logins/attempts/{aid}/code", json={"code": CODE})
    assert r.status_code == 410 and r.json() == {"detail": EXPIRED}
    assert attempt(db_session, aid).code_enc is None


def test_worker_expires_waiting_attempt(client, db_session, session_factory, monkeypatch):
    c = mk_login(db_session, status="ok")
    fake = FakeSignIn(monkeypatch)
    aid = start(client, c.id)

    def on_wait():  # ten minutes pass with no code
        db_session.execute(update(LoginAttempt).where(LoginAttempt.id == aid).values(
            waiting_since=datetime.now(timezone.utc) - timedelta(minutes=10, seconds=1)))
        db_session.commit()

    fake.on_wait = on_wait
    work(session_factory)
    a = attempt(db_session, aid)
    assert a.status == "expired" and a.error == EXPIRED and a.code_enc is None
    row = ls(db_session, c.id)
    assert row.status == "login_needed" and row.last_error == EXPIRED


def test_wrong_code_failed(client, db_session, session_factory, monkeypatch):
    c = mk_login(db_session)
    src = mk_source(db_session, "custom-w")
    add_target(db_session, src, c)
    fake = FakeSignIn(monkeypatch, accept="000000")
    FakeFetch(monkeypatch)
    aid = start(client, c.id)
    fake.on_wait = lambda: client.post(f"/api/logins/attempts/{aid}/code", json={"code": CODE})
    work(session_factory)
    a = attempt(db_session, aid)
    assert a.status == "failed" and a.code_enc is None
    assert a.error.startswith("code was not accepted")
    assert CODE not in a.error and PASSWORD not in a.error
    row = ls(db_session, c.id)
    assert row.status == "login_needed" and row.last_error == a.error and row.state_enc is None
    assert row.notified_at is not None  # the user saw it: no Mattermost alert for it later
    assert queued(db_session, src) == []
    r = client.get(f"/api/logins/{c.id}/attempt").json()
    assert r["status"] == "failed" and r["error"] == a.error
    assert login_status(client, c.id) == "login_needed"


def test_cancel_wipes_code(client, db_session, session_factory, monkeypatch):
    c = mk_login(db_session, status="login_needed", last_error="earlier problem")
    fake = FakeSignIn(monkeypatch)
    aid = start(client, c.id)
    seen = {}

    def on_wait():
        client.post(f"/api/logins/attempts/{aid}/code", json={"code": CODE})
        assert attempt(db_session, aid).code_enc is not None
        r = client.post(f"/api/logins/attempts/{aid}/cancel")
        seen["cancel"] = (r.status_code, r.json())
        a = attempt(db_session, aid)
        seen["after"] = (a.status, a.code_enc, a.finished_at is not None)

    fake.on_wait = on_wait
    work(session_factory)
    assert seen["cancel"] == (200, {"attempt_id": aid, "status": "cancelled"})
    assert seen["after"] == ("cancelled", None, True)
    a = attempt(db_session, aid)
    assert a.status == "cancelled" and a.code_enc is None and a.error is None  # not overwritten
    row = ls(db_session, c.id)  # as before the attempt
    assert row.status == "login_needed" and row.last_error == "earlier problem"
    r = client.post(f"/api/logins/attempts/{aid}/cancel")
    assert r.status_code == 409
    assert client.post("/api/logins/attempts/999999/cancel").status_code == 404


def test_key_edit_cancels_attempt_and_is_not_overwritten(client, db_session, session_factory,
                                                         monkeypatch):
    c = mk_login(db_session, status="login_needed", last_error="x", notified=True)
    fake = FakeSignIn(monkeypatch)
    FakeFetch(monkeypatch)
    aid = start(client, c.id)

    def on_wait():
        client.post(f"/api/logins/attempts/{aid}/code", json={"code": CODE})
        r = client.patch(f"/api/credentials/{c.id}", json={"secret": "another-password-1"})
        assert r.status_code == 200

    fake.on_wait = on_wait
    work(session_factory)
    a = attempt(db_session, aid)
    assert a.status == "cancelled" and a.code_enc is None
    row = ls(db_session, c.id)  # the reset stands: no session, no error, alert re-armed (M5)
    assert row.status == "login_needed" and row.last_error is None
    assert row.state_enc is None and row.notified_at is None


def test_cancel_while_signing_in_discards_session(client, db_session, session_factory,
                                                  monkeypatch):
    c = mk_login(db_session, status="ok")
    fake = FakeSignIn(monkeypatch, needs_code=False)
    aid = start(client, c.id)
    fake.on_start = lambda: client.post(f"/api/logins/attempts/{aid}/cancel")
    work(session_factory)
    assert attempt(db_session, aid).status == "cancelled"
    row = ls(db_session, c.id)
    assert row.status == "ok" and row.state_enc is None  # nothing saved, status restored


def test_lock_busy_waits_then_fails(client, db_session, session_factory, monkeypatch, db_engine):
    c = mk_login(db_session, status="ok")
    fake = FakeSignIn(monkeypatch)
    monkeypatch.setattr(la, "LOCK_WAIT_S", 0.3)
    aid = start(client, c.id)
    other = db_engine.connect()  # an automatic sign-in holds the login's lock
    try:
        other.execute(text("SELECT pg_advisory_lock(:ns, :cid)"),
                      {"ns": logins.LOGIN_LOCK_NAMESPACE, "cid": c.id})
        work(session_factory)
    finally:
        other.close()
    a = attempt(db_session, aid)
    assert a.status == "failed" and a.error == BUSY
    assert fake.calls == 0 and ls(db_session, c.id).status == "ok"


@respx.mock
def test_verification_still_login_page_fails(client, db_session, session_factory, monkeypatch):
    robots()
    c = mk_login(db_session)
    src = mk_source(db_session, "custom-w")
    add_target(db_session, src, c)
    FakeSignIn(monkeypatch, needs_code=False)
    FakeFetch(monkeypatch, expired=True)
    aid = start(client, c.id)
    work(session_factory)
    a = attempt(db_session, aid)
    assert a.status == "failed" and a.error == STILL_FORM
    row = ls(db_session, c.id)
    assert row.status == "login_needed" and row.last_error == STILL_FORM and row.state_enc is None
    assert queued(db_session, src) == []


def test_sweep_stale_attempts(db_session, session_factory, db_engine):
    now = datetime.now(timezone.utc)
    logins_ = [mk_login(db_session, name=f"L{i}") for i in range(6)]
    rows = [
        LoginAttempt(credential_id=logins_[0].id, status="waiting_code",
                     code_enc=creds.encrypt(CODE), waiting_since=now),
        LoginAttempt(credential_id=logins_[1].id, status="pending"),
        LoginAttempt(credential_id=logins_[2].id, status="signing_in"),  # lock held: alive
        LoginAttempt(credential_id=logins_[3].id, status="verifying",
                     created_at=now - timedelta(minutes=20)),  # lock held but too old
        LoginAttempt(credential_id=logins_[4].id, status="succeeded"),
    ]
    db_session.add_all(rows)
    s4 = db_session.get(LoginSession, logins_[4].id)
    s4.status, s4.last_error = "signing_in", "kept"  # stuck automatic sign-in (Task 3 crash)
    db_session.get(LoginSession, logins_[2].id).status = "waiting_code"  # alive: untouched
    db_session.get(LoginSession, logins_[0].id).status = "waiting_code"
    db_session.commit()
    cids = [x.id for x in logins_]
    db_session.rollback()  # no read transaction left open around the sweep's writes
    held = db_engine.connect()
    try:
        for i in (2, 3):
            held.execute(text("SELECT pg_advisory_lock(:ns, :cid)"),
                         {"ns": logins.LOGIN_LOCK_NAMESPACE, "cid": cids[i]})
        assert la.sweep_stale_attempts(session_factory) == 3
    finally:
        held.close()
    db_session.expire_all()
    got = [(r.status, r.error, r.code_enc) for r in rows]
    assert got[0] == ("failed", RESTARTED, None)
    assert got[1] == ("failed", RESTARTED, None)
    assert got[2] == ("signing_in", None, None)
    assert got[3] == ("failed", RESTARTED, None)
    assert got[4] == ("succeeded", None, None)
    assert [ls(db_session, cid).status for cid in cids] == [
        "login_needed", "login_needed", "waiting_code", "login_needed", "login_needed",
        "login_needed"]
    assert ls(db_session, cids[4]).last_error == "kept"


def test_poller_runs_at_most_two_attempts(client, db_session, session_factory):
    cs = [mk_login(db_session, name=f"L{i}") for i in range(3)]
    ids = [start(client, c.id) for c in cs]
    gate, running = threading.Event(), []

    def runner(sf, aid):
        running.append(aid)
        gate.wait(10)

    w = la.AttemptWorker(session_factory, run=runner)
    try:
        assert w.poll() == 2
        assert w.poll() == 0  # both slots busy
        deadline = time.monotonic() + 5
        while len(running) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert sorted(running) == ids[:2]
        assert attempt(db_session, ids[2]).status == "pending"
    finally:
        gate.set()
        w.join(5)
    assert w.poll() == 1


def test_code_never_in_responses_or_logs(client, db_session, session_factory, monkeypatch,
                                         caplog):
    caplog.set_level(logging.DEBUG)
    c = mk_login(db_session)
    fake = FakeSignIn(monkeypatch)
    texts = []
    aid = start(client, c.id)

    def on_wait():
        texts.append(client.post(f"/api/logins/attempts/{aid}/code", json={"code": CODE}).text)
        texts.append(client.get(f"/api/logins/{c.id}/attempt").text)
        texts.append(client.get("/api/credentials").text)

    fake.on_wait = on_wait
    fake.accept = "nope"  # the engine's error carries the code and the password
    work(session_factory)
    texts.append(client.get(f"/api/logins/{c.id}/attempt").text)
    texts.append(client.get("/api/credentials").text)
    texts.append(client.post(f"/api/logins/attempts/{aid}/code", json={"code": CODE}).text)
    texts.append(client.post(f"/api/logins/attempts/{aid}/code", json={"code": "a b"}).text)
    blob = "\n".join(texts) + caplog.text
    assert CODE not in blob and PASSWORD not in blob and "a b" not in blob
    a = attempt(db_session, aid)
    assert a.status == "failed" and "***" in a.error
    assert CODE not in ls(db_session, c.id).last_error


# --- carried from the Task 3 review ----------------------------------------------------------

class AutoSite:
    """logins.sign_in succeeds without a code, but every page still shows the login form."""

    def __init__(self, monkeypatch):
        self.sign_ins = 0
        monkeypatch.setattr(logins, "browser_fetch", self.fetch)
        monkeypatch.setattr(logins, "sign_in", self.sign_in)

    def fetch(self, url, cred, state, kind="page"):
        return FetchResult("login_needed", error=logins.LOGIN_NEEDED), state, True

    def sign_in(self, cred, code_provider=None, timeout_s=600):
        self.sign_ins += 1
        return sid_state(f"s{self.sign_ins}")


class Rec:
    def __init__(self, fail=False):
        self.fail = fail
        self.extras: list[dict] = []

    def notify_run(self, run, source, top, streak, extras=None):
        self.extras.append(extras or {})
        if self.fail:
            raise RuntimeError("mattermost down")

    def alerts(self) -> int:
        return sum(len(e.get("login_needed") or []) for e in self.extras)


@respx.mock
def test_auto_sign_in_still_login_page_marks_login_needed_once(db_session, session_factory,
                                                               monkeypatch):
    robots()
    c = mk_login(db_session, status="ok")
    src = mk_source(db_session, "custom-w")
    add_target(db_session, src, c, "https://portal.example/a", "A")
    add_target(db_session, src, c, "https://portal.example/b", "B")
    site, rec = AutoSite(monkeypatch), Rec()
    pipeline.run_source(session_factory, "custom-w", notifier=rec)
    row = ls(db_session, c.id)
    assert row.status == "login_needed" and row.last_error == STILL_FORM
    assert site.sign_ins == 1 and rec.alerts() == 1
    pipeline.run_source(session_factory, "custom-w", notifier=rec)
    assert site.sign_ins == 1 and rec.alerts() == 1  # no retry loop, no second alert


@respx.mock
def test_failed_alert_delivery_re_alerts_next_run(db_session, session_factory, monkeypatch):
    robots()
    c = mk_login(db_session, status="ok")
    src = mk_source(db_session, "custom-w")
    add_target(db_session, src, c)
    AutoSite(monkeypatch)
    down = Rec(fail=True)
    r = pipeline.run_source(session_factory, "custom-w", notifier=down)
    assert r.notify_error and down.alerts() == 1
    assert ls(db_session, c.id).notified_at is None  # the alert was not delivered
    rec = Rec()
    pipeline.run_source(session_factory, "custom-w", notifier=rec)
    assert rec.alerts() == 1
    assert ls(db_session, c.id).notified_at is not None
    pipeline.run_source(session_factory, "custom-w", notifier=rec)
    assert rec.alerts() == 1  # delivered once: no more


def test_key_reset_clears_notified_at(client, db_session):
    c = mk_login(db_session, status="login_needed", last_error="x", notified=True)
    r = client.patch(f"/api/credentials/{c.id}", json={"username": "new@example.com"})
    assert r.status_code == 200
    row = ls(db_session, c.id)
    assert row.notified_at is None and row.last_error is None


# --- real browser ----------------------------------------------------------------------------

def _chromium_available() -> bool:
    try:
        import os

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            exe = os.environ.get("KPI_CHROMIUM_PATH")
            b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
            b.close()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _chromium_available(), reason="Chromium not launchable")
def test_real_browser_code_flow(db_engine, tmp_path, monkeypatch, trust_test_cert):
    """Real engine, real worker thread, real (committed) database rows; the code is read from
    the file the site 'emails' it to and submitted through the API while the thread waits."""
    from tests.login_site import PROTECTED, LoginSite

    monkeypatch.setattr(get_settings(), "watch_allowed_hosts", "127.0.0.1,localhost")
    site = LoginSite(USER, PASSWORD, code_file=tmp_path / "code.txt")
    site.code_mode = True
    trust_test_cert(site.spki)
    sf = sessionmaker(bind=db_engine, expire_on_commit=False)

    def get_session():
        with sf() as s:
            yield s

    client = make_client(get_session, sf, monkeypatch)
    with sf() as s:
        cred = Credential(name="Real portal", kind="login", username=USER,
                          secret_enc=creds.encrypt(PASSWORD), login_url=site.url("/login"))
        s.add(cred)
        s.flush()
        s.add(LoginSession(credential_id=cred.id, status="login_needed"))
        src = Source(slug="custom-real-login", name="Real", area="custom", access="watch")
        s.add(src)
        s.flush()
        s.add(WatchTarget(source_id=src.id, kpi_name="Data", url=site.url("/data"), kind="page",
                          use_browser=True, credential_id=cred.id))
        s.commit()
        cid, sid = cred.id, src.id
    worker = la.AttemptWorker(sf)
    try:
        aid = start(client, cid)
        assert worker.poll() == 1

        def wait_for(statuses, timeout=90):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                body = client.get(f"/api/logins/{cid}/attempt").json()
                if body["status"] in statuses:
                    return body
                time.sleep(0.2)
            raise AssertionError(f"attempt still {body}")

        body = wait_for({"waiting_code", "failed"})
        assert body["status"] == "waiting_code", body
        code = (tmp_path / "code.txt").read_text()
        r = client.post(f"/api/logins/attempts/{aid}/code", json={"code": code})
        assert r.status_code == 202 and code not in r.text
        body = wait_for({"succeeded", "failed", "expired", "cancelled"})
        assert body["status"] == "succeeded", body
        worker.join(30)
        with sf() as s:
            row = s.get(LoginSession, cid)
            assert row.status == "ok" and row.last_error is None
            state = json.loads(creds.decrypt(row.state_enc))
            assert any(c["name"] == "sid" for c in state["cookies"])
            a = s.get(LoginAttempt, aid)
            assert a.code_enc is None
            runs = s.scalars(select(Run).where(Run.source_id == sid)).all()
            assert [(r.status, r.trigger) for r in runs] == [("queued", "manual")]
            loaded = logins.load_state(s, creds.load(s, cid))
        res, _, expired = logins.browser_fetch(site.url("/data"), creds.Credential(
            cid, "Real portal", "login", None, None, None, USER, PASSWORD, site.url("/login")),
            loaded)
        assert not expired and PROTECTED in res.body.decode()
    finally:
        worker.join(30)
        site.close()
        with sf() as s:
            s.execute(text("DELETE FROM runs WHERE source_id = :s"), {"s": sid})
            s.execute(text("DELETE FROM watch_targets WHERE source_id = :s"), {"s": sid})
            s.execute(text("DELETE FROM sources WHERE id = :s"), {"s": sid})
            s.execute(text("DELETE FROM credentials WHERE id = :c"), {"c": cid})
            s.commit()
