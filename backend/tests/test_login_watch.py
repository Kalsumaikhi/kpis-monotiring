"""Watch KPIs behind browser logins (Logins plan Task 3).

Most tests fake `logins.browser_fetch` / `logins.sign_in` (FakeSite) so the connector's
grouping, re-login, status, compare-and-set and alert logic run without a browser. The tests
marked `browser` run the real engine against tests/login_site.py and skip without Chromium.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app import credentials as creds
from app import logins
from app.api import deps
from app.api.main import create_app
from app.config import get_settings
from app.connectors.watch import fetch as fetch_mod
from app.connectors.watch.connector import _process
from app.connectors.watch.fetch import FetchResult, fetch_target
from app.core import pipeline
from app.models import (
    Credential,
    DataPoint,
    LoginSession,
    Run,
    Source,
    WatchSnapshot,
    WatchTarget,
)
from app.notify.mattermost import format_run_message

AUTH = ("anyone", "secret")
USER = "me@example.com"
PASSWORD = "Sup3r-secret-pass!"
LOGIN_URL = "https://portal.example/login"
HOST_MSG = 'this login is for portal.example; add the KPI\'s site under "Also used on"'
ALERT = ("**Watch** — Portal needs you to sign in. Open the dashboard to enter the email code."
         "\n[Sign in](http://dash/settings#logins)")


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("KPI_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(fetch_mod, "MIN_INTERVAL", 0.0)
    real = fetch_mod.socket.getaddrinfo

    def fake(host, *a, **k):
        if host in ("localhost", "127.0.0.1"):
            return real(host, *a, **k)
        if host == "intranet.portal.example":
            return [(2, 1, 6, "", ("10.0.0.7", 0))]
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(fetch_mod.socket, "getaddrinfo", fake)


@pytest.fixture
def client(db_session, session_factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "dashboard_password", "secret")
    app = create_app()
    app.dependency_overrides[deps.get_session] = lambda: db_session
    app.dependency_overrides[deps.get_session_factory] = lambda: session_factory
    c = TestClient(app)
    c.auth = AUTH
    return c


def mk_login(db_session, name="Portal", login_url=LOGIN_URL, extra_hosts=None,
             status="login_needed", state=None):
    row = Credential(name=name, kind="login", username=USER, secret_enc=creds.encrypt(PASSWORD),
                     login_url=login_url, extra_hosts=extra_hosts)
    db_session.add(row)
    db_session.flush()
    db_session.add(LoginSession(
        credential_id=row.id, status=status,
        state_enc=creds.encrypt(json.dumps(state)) if state is not None else None))
    db_session.flush()
    return row


def mk_header_key(db_session, name="Hdr"):
    row = Credential(name=name, kind="header", header_name="X-Key",
                     secret_enc=creds.encrypt("header-secret-123"))
    db_session.add(row)
    db_session.flush()
    return row


def robots(disallow: bool = False):
    body = "User-agent: *\nDisallow: /\n" if disallow else ""
    respx.get(url__regex=r"https://[a-z.]+/robots\.txt").mock(
        return_value=httpx.Response(200 if disallow else 404, text=body))


def sid_state(sid: str) -> dict:
    return {"cookies": [{"name": "sid", "value": sid, "domain": "portal.example", "path": "/",
                         "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"}],
            "origins": []}


class FakeSite:
    """Stands in for app.logins.browser_fetch / sign_in."""

    def __init__(self, monkeypatch, valid=("s1",)):
        self.valid = set(valid)
        self.code_mode = False
        self.fail = False
        self.fetches: list[tuple[str, dict | None]] = []
        self.sign_ins = 0
        self.content = {}  # url -> text
        self.echo_password = False
        self.on_fetch = None
        self.on_sign_in = None
        self.kinds: list[str] = []
        self._n = 0
        monkeypatch.setattr(logins, "browser_fetch", self.browser_fetch)
        monkeypatch.setattr(logins, "sign_in", self.sign_in)

    def browser_fetch(self, url, cred, state, kind="page"):
        self.fetches.append((url, state))
        self.kinds.append(kind)
        if self.on_fetch:
            self.on_fetch(url)
        sids = {c["value"] for c in (state or {}).get("cookies", [])}
        if not sids & self.valid:
            return FetchResult("login_needed", error=logins.LOGIN_NEEDED), state, True
        text = self.content.get(url, "v1")
        extra = f"<p>hello {PASSWORD}</p>" if self.echo_password else ""
        body = f"<html><body><main>{text}</main>{extra}</body></html>".encode()
        rotated = json.loads(json.dumps(state))
        rotated["cookies"].append({"name": "rot", "value": f"r{len(self.fetches)}",
                                   "domain": "portal.example", "path": "/"})
        return FetchResult("ok", "text/html; charset=utf-8", body), rotated, False

    def sign_in(self, cred, code_provider=None, timeout_s=600):
        assert code_provider is None
        self.sign_ins += 1
        if self.on_sign_in:
            self.on_sign_in()
        if self.fail:
            raise logins.LoginFailed(creds.redact(f"login failed: bad {cred.secret}", cred))
        if self.code_mode:
            raise logins.LoginNeedsCode("the site asks for the emailed code")
        self._n += 1
        sid = f"new{self._n}"
        self.valid.add(sid)
        return sid_state(sid)


@pytest.fixture
def wenv(db_session, session_factory):
    src = Source(slug="custom-w", name="Watch", area="custom", access="watch")
    db_session.add(src)
    db_session.flush()
    return src, session_factory


def add_target(db_session, src, cred, url="https://portal.example/a", name="A", use_browser=True):
    t = WatchTarget(source_id=src.id, kpi_name=name, url=url, kind="page",
                    use_browser=use_browser, credential_id=cred.id if cred else None)
    db_session.add(t)
    db_session.flush()
    return t


class Rec:
    def __init__(self):
        self.messages: list[str] = []
        self.extras: list[dict] = []

    def notify_run(self, run, source, top, streak, extras=None):
        self.extras.append(extras or {})
        msg = format_run_message(run, source, top, streak, dashboard_base_url="http://dash",
                                 consecutive_failure_alert=3, extras=extras)
        if msg is not None:
            self.messages.append(msg)


def session_row(db_session, cid) -> LoginSession:
    db_session.expire_all()
    return db_session.get(LoginSession, cid)


def stored_state(db_session, cid) -> dict | None:
    row = session_row(db_session, cid)
    return json.loads(creds.decrypt(row.state_enc)) if row.state_enc else None


def run(sf, rec=None):
    return pipeline.run_source(sf, "custom-w", notifier=rec)


# --- API --------------------------------------------------------------------------------

def create(client, **extra):
    return client.post("/api/custom-sources", json={
        "source_name": "Watch", "kpi_name": "Price", "url": "https://portal.example/p", **extra})


def sheet(rows):
    data = "Source name,KPI name,Link,Key name\n" + "\n".join(rows) + "\n"
    return {"file": ("t.csv", io.BytesIO(data.encode()), "text/csv")}


def test_login_target_forced_browser(client, db_session):
    c = mk_login(db_session)
    r = create(client, credential_id=c.id)
    assert r.status_code == 201, r.text
    assert r.json()["use_browser"] is True
    tid = r.json()["id"]
    r = client.patch(f"/api/watch-targets/{tid}", json={"use_browser": False})
    assert r.status_code == 200 and r.json()["use_browser"] is True
    # Moving the KPI to a non-login key drops the forced browser mode with it.
    h = mk_header_key(db_session)
    r = client.patch(f"/api/watch-targets/{tid}", json={"credential_id": h.id})
    assert r.status_code == 200 and r.json()["use_browser"] is False
    # Non-login keys still cannot be combined with browser mode.
    r = client.patch(f"/api/watch-targets/{tid}", json={"use_browser": True})
    assert r.status_code == 422
    assert r.json()["detail"] == 'keys can\'t be used with "Needs a browser" yet'
    r = client.patch(f"/api/watch-targets/{tid}", json={"credential_id": c.id})
    assert r.status_code == 200 and r.json()["use_browser"] is True
    # Import: a login key name makes the row a browser KPI.
    p = client.post("/api/watch-targets/import/preview",
                    files=sheet(["Imp,K1,https://portal.example/k1,portal"])).json()
    assert p["rows"][0]["status"] == "new_source" and p["rows"][0]["use_browser"] is True
    client.post("/api/watch-targets/import/confirm", json={"token": p["token"]})
    t = db_session.scalars(select(WatchTarget).where(WatchTarget.kpi_name == "K1")).one()
    assert t.use_browser is True and t.credential_id == c.id


def test_login_host_must_match_422(client, db_session):
    c = mk_login(db_session, extra_hosts=["data.portal.example"])
    r = create(client, credential_id=c.id, url="https://other.example/p")
    assert r.status_code == 422 and r.json() == {"detail": HOST_MSG}
    r = create(client, credential_id=c.id, url="http://portal.example/p")
    assert r.status_code == 422
    ok = create(client, credential_id=c.id, url="https://data.portal.example/p")
    assert ok.status_code == 201
    r = client.patch(f"/api/watch-targets/{ok.json()['id']}",
                     json={"url": "https://other.example/q"})
    assert r.status_code == 422 and r.json() == {"detail": HOST_MSG}
    r = client.post("/api/watch-targets/test",
                    json={"url": "https://other.example/q", "credential_id": c.id})
    assert r.status_code == 422 and r.json() == {"detail": HOST_MSG}
    p = client.post("/api/watch-targets/import/preview",
                    files=sheet(["Imp,K2,https://other.example/k2,Portal"])).json()
    assert p["rows"][0]["status"] == "invalid" and p["rows"][0]["error"] == HOST_MSG


def test_import_confirm_rechecks_login_host(client, db_session):
    c = mk_login(db_session, extra_hosts=["data.portal.example"])
    p = client.post("/api/watch-targets/import/preview",
                    files=sheet(["Imp,K3,https://data.portal.example/k3,Portal"])).json()
    assert p["rows"][0]["status"] == "new_source"
    db_session.get(Credential, c.id).extra_hosts = None  # host removed after the preview
    db_session.flush()
    out = client.post("/api/watch-targets/import/confirm", json={"token": p["token"]}).json()
    assert out["created_targets"] == 0 and out["skipped"] == 1


def test_switching_used_key_into_login_409(client, db_session, wenv):
    src, _ = wenv
    login = {"kind": "login", "login_url": LOGIN_URL, "username": USER}
    h = mk_header_key(db_session)
    add_target(db_session, src, h, use_browser=False)
    r = client.patch(f"/api/credentials/{h.id}", json=login)
    assert r.status_code == 409 and r.json() == {"detail": "this key is used by KPIs"}
    h2 = mk_header_key(db_session, name="Hdr2")
    s2 = Source(slug="static-x", name="Static", area="a", access="api", credential_id=h2.id)
    db_session.add(s2)
    db_session.flush()
    r = client.patch(f"/api/credentials/{h2.id}", json=login)
    assert r.status_code == 409 and r.json() == {"detail": "this key is used by KPIs"}
    h3 = mk_header_key(db_session, name="Hdr3")
    assert client.patch(f"/api/credentials/{h3.id}", json=login).status_code == 200


def test_switching_used_login_to_other_kind_409(client, db_session, wenv):
    src, _ = wenv
    c = mk_login(db_session)
    add_target(db_session, src, c)
    r = client.patch(f"/api/credentials/{c.id}", json={"kind": "header", "header_name": "X-K"})
    assert r.status_code == 409 and r.json() == {"detail": "this login is used by KPIs"}
    unused = mk_login(db_session, name="Spare")
    r = client.patch(f"/api/credentials/{unused.id}",
                     json={"kind": "header", "header_name": "X-K"})
    assert r.status_code == 200


def test_test_link_with_login_uses_stored_session_only(client, db_session, monkeypatch):
    site = FakeSite(monkeypatch)
    c = mk_login(db_session, status="ok", state=sid_state("old"))
    with respx.mock:
        robots()
        r = client.post("/api/watch-targets/test",
                        json={"url": "https://portal.example/p", "credential_id": c.id})
    assert r.status_code == 200
    assert r.json() == {"status": "login_needed", "kind": "auto", "error": "login needed"}
    assert site.sign_ins == 0
    assert site.fetches[0][1]["cookies"][0]["value"] == "old"
    site.valid.add("old")
    with respx.mock:
        robots()
        r = client.post("/api/watch-targets/test",
                        json={"url": "https://portal.example/p", "credential_id": c.id,
                              "kind": "page"})
    assert r.json()["status"] == "ok" and "v1" in r.json()["text_preview"]
    assert stored_state(db_session, c.id) == sid_state("old")  # nothing written


def test_fetch_target_refuses_login_key():
    cred = creds.Credential(1, "Portal", "login", None, None, None, USER, PASSWORD,
                            login_url=LOGIN_URL)
    with httpx.Client() as c:
        res = fetch_target(c, "https://portal.example/p", True, fetch_mod.FetchState(),
                           credential=cred)
    assert res.status == "error"


# --- scheduled runs (fake browser) -------------------------------------------------------

@respx.mock
def test_scheduled_run_auto_relogin_without_code(wenv, db_session, monkeypatch):
    src, sf = wenv
    site = FakeSite(monkeypatch, valid=())
    c = mk_login(db_session, status="ok", state=sid_state("expired"))
    add_target(db_session, src, c, "https://portal.example/a", "A")
    add_target(db_session, src, c, "https://portal.example/b", "B")
    robots()
    r = run(sf)
    assert r.status == "baseline", r.error
    assert site.sign_ins == 1
    # first fetch with the old session, then a refetch and the second KPI with the new one
    assert [s["cookies"][0]["value"] for _, s in site.fetches] == ["expired", "new1", "new1"]
    state = stored_state(db_session, c.id)
    assert state["cookies"][0]["value"] == "new1"
    assert any(ck["name"] == "rot" for ck in state["cookies"])  # refreshed state saved
    row = session_row(db_session, c.id)
    assert row.status == "ok" and row.last_error is None
    ts = db_session.scalars(select(WatchTarget).order_by(WatchTarget.id)).all()
    assert [t.last_status for t in ts] == ["ok", "ok"]


@respx.mock
def test_code_required_marks_login_needed_incomplete(wenv, db_session, monkeypatch):
    src, sf = wenv
    site = FakeSite(monkeypatch)
    c = mk_login(db_session, status="ok", state=sid_state("s1"))
    for n in ("A", "B", "C"):
        add_target(db_session, src, c, f"https://portal.example/{n.lower()}", n)
    add_target(db_session, src, None, "https://open.example/x", "Open", use_browser=False)
    robots()
    respx.get("https://open.example/x").mock(return_value=httpx.Response(
        200, text="<main>open</main>", headers={"content-type": "text/html"}))
    assert run(sf).status == "baseline"
    db_session.expire_all()
    before = db_session.scalar(select(func.count()).select_from(DataPoint))
    assert before == 4
    site.valid.clear()
    site.code_mode = True
    rec = Rec()
    r = run(sf, rec)
    assert r.status == "no_change"
    assert r.error.startswith("incomplete: 3 KPI(s) not checked: A (login_needed: login needed)")
    assert site.sign_ins == 1  # once per login per run
    assert len(site.fetches) == 3 + 1  # baseline 3, then one expired fetch; no more browser work
    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(DataPoint)) == before
    ts = {t.kpi_name: t for t in db_session.scalars(select(WatchTarget))}
    for n in ("A", "B", "C"):
        assert (ts[n].last_status, ts[n].last_error) == ("login_needed", "login needed")
    assert ts["Open"].last_status == "ok"
    row = session_row(db_session, c.id)
    assert row.status == "login_needed"
    assert row.last_error == "the site asks for the emailed code"
    assert row.notified_at is not None
    assert stored_state(db_session, c.id)["cookies"][0]["value"] == "s1"  # session kept
    assert rec.extras[0]["login_needed"] == [{"login_name": "Portal", "source": "Watch",
                                              "credential_id": c.id}]
    assert rec.messages[-1].endswith(ALERT)


@respx.mock
def test_login_needed_alert_once(wenv, db_session, monkeypatch):
    src, sf = wenv
    site = FakeSite(monkeypatch, valid=())
    site.code_mode = True
    c = mk_login(db_session)
    for n in ("A", "B", "C"):
        add_target(db_session, src, c, f"https://portal.example/{n.lower()}", n)
    robots()
    rec = Rec()
    r1 = run(sf, rec)
    r2 = run(sf, rec)
    assert r1.status == r2.status == "failed"  # every KPI needed the login
    assert sum(m.count("needs you to sign in") for m in rec.messages) == 1
    assert rec.messages[0].endswith(ALERT)
    assert "check failed" in rec.messages[0]
    assert site.sign_ins == 1  # tried once; never again after a code was asked for
    db_session.expire_all()
    assert all(t.last_status == "login_needed" for t in db_session.scalars(select(WatchTarget)))


@respx.mock
def test_alert_again_after_recovery_then_expiry(wenv, db_session, monkeypatch):
    src, sf = wenv
    site = FakeSite(monkeypatch, valid=())
    site.code_mode = True
    c = mk_login(db_session, status="ok", state=sid_state("gone"))
    add_target(db_session, src, c, "https://portal.example/a", "A")
    add_target(db_session, src, None, "https://open.example/x", "Open", use_browser=False)
    robots()
    respx.get("https://open.example/x").mock(return_value=httpx.Response(
        200, text="<main>open</main>", headers={"content-type": "text/html"}))
    rec = Rec()
    run(sf, rec)
    assert sum(m.count("needs you to sign in") for m in rec.messages) == 1
    # The user signs in from the dashboard (Task 4): a fresh session, status ok.
    with sf() as s:
        s.execute(update(LoginSession).where(LoginSession.credential_id == c.id).values(
            status="ok", last_error=None,
            state_enc=creds.encrypt(json.dumps(sid_state("fresh")))))
        s.commit()
    site.valid.add("fresh")
    assert run(sf, rec).status in ("ok", "no_change", "baseline")
    assert session_row(db_session, c.id).status == "ok"
    run(sf, rec)  # still fine: no alert
    assert sum(m.count("needs you to sign in") for m in rec.messages) == 1
    site.valid.clear()
    site.code_mode = True
    run(sf, rec)
    assert sum(m.count("needs you to sign in") for m in rec.messages) == 2


# --- R-a: when an automatic sign-in may be tried ----------------------------------------

@respx.mock
def test_no_auto_sign_in_after_code_or_failure(wenv, db_session, monkeypatch):
    src, sf = wenv
    site = FakeSite(monkeypatch, valid=())
    c = mk_login(db_session, status="login_needed", state=sid_state("old"))
    with sf() as s:
        s.execute(update(LoginSession).where(LoginSession.credential_id == c.id)
                  .values(last_error="login failed: still on the login page",
                          notified_at=func.now()))  # alerted when it failed
        s.commit()
    add_target(db_session, src, c, "https://portal.example/a", "A")
    add_target(db_session, src, c, "https://portal.example/b", "B")
    robots()
    before = session_row(db_session, c.id)
    before = (before.status, bytes(before.state_enc), before.last_error, before.updated_at)
    rec = Rec()
    for _ in range(2):
        r = run(sf, rec)
        assert r.status == "failed"
    assert site.sign_ins == 0
    assert len(site.fetches) == 2  # one expired fetch per run, then nothing more
    after = session_row(db_session, c.id)
    assert (after.status, bytes(after.state_enc), after.last_error, after.updated_at) == before
    assert not any("needs you to sign in" in m for m in rec.messages)


@respx.mock
def test_reset_login_gets_exactly_one_attempt(wenv, db_session, monkeypatch, client):
    src, sf = wenv
    site = FakeSite(monkeypatch, valid=())
    site.fail = True
    c = mk_login(db_session)  # fresh: login_needed, no error
    add_target(db_session, src, c)
    robots()
    run(sf)
    run(sf)
    assert site.sign_ins == 1
    assert session_row(db_session, c.id).last_error.startswith("login failed")
    # Editing the password resets the session: one more automatic try is allowed.
    r = client.patch(f"/api/credentials/{c.id}", json={"secret": "an0ther-passw0rd"})
    assert r.status_code == 200
    site.fail = False
    assert run(sf).status in ("baseline", "ok", "no_change")
    assert site.sign_ins == 2
    assert session_row(db_session, c.id).status == "ok"


@respx.mock
def test_ok_login_expired_signs_in_and_status_is_signing_in_meanwhile(
        wenv, db_session, monkeypatch):
    src, sf = wenv
    site = FakeSite(monkeypatch, valid=())
    c = mk_login(db_session, status="ok", state=sid_state("old"))
    add_target(db_session, src, c)
    seen = []

    def peek():
        with sf() as s:
            seen.append(s.get(LoginSession, c.id).status)

    site.on_sign_in = peek
    robots()
    run(sf)
    assert seen == ["signing_in"] and session_row(db_session, c.id).status == "ok"
    # A crash inside the attempt restores the earlier status (here: after expiry again).
    site.valid.clear()

    def boom():
        raise RuntimeError("browser died")

    site.on_sign_in = boom
    run(sf)
    row = session_row(db_session, c.id)
    assert row.status == "login_needed"
    assert row.last_error == "login failed: RuntimeError"


@respx.mock
def test_sign_in_lock_busy_skips_without_touching_session(wenv, db_session, monkeypatch,
                                                          db_engine):
    from sqlalchemy import text

    src, sf = wenv
    site = FakeSite(monkeypatch, valid=())
    c = mk_login(db_session, status="ok", state=sid_state("old"))
    add_target(db_session, src, c, "https://portal.example/a", "A")
    add_target(db_session, src, c, "https://portal.example/b", "B")
    robots()
    before = session_row(db_session, c.id)
    before = (before.status, bytes(before.state_enc), before.updated_at)
    other = db_engine.connect()
    params = {"ns": logins.LOGIN_LOCK_NAMESPACE, "cid": c.id}
    other.execute(text("SELECT pg_advisory_lock(:ns, :cid)"), params)
    try:
        r = run(sf)
    finally:
        other.execute(text("SELECT pg_advisory_unlock(:ns, :cid)"), params)
        other.close()
    assert site.sign_ins == 0 and len(site.fetches) == 1
    assert r.status == "failed" and "login_needed: login needed" in r.error
    after = session_row(db_session, c.id)
    assert (after.status, bytes(after.state_enc), after.updated_at) == before


def test_login_lock_is_exclusive(db_engine):
    with logins.login_lock(db_engine, 987654):
        with pytest.raises(logins.LoginBusy), logins.login_lock(db_engine, 987654):
            pass
        with logins.login_lock(db_engine, 987655):  # another login is independent
            pass
    with logins.login_lock(db_engine, 987654):  # released
        pass


@respx.mock
def test_kind_passed_to_browser_fetch(wenv, db_session, monkeypatch):
    src, sf = wenv
    site = FakeSite(monkeypatch)
    c = mk_login(db_session, status="ok", state=sid_state("s1"))
    t = add_target(db_session, src, c, "https://portal.example/a", "A")
    t.kind = "api"
    db_session.flush()
    robots()
    run(sf)
    assert site.kinds == ["api"]


@respx.mock
def test_login_failed_redacted_everywhere(wenv, db_session, monkeypatch, client):
    src, sf = wenv
    site = FakeSite(monkeypatch, valid=())
    site.fail = True
    c = mk_login(db_session)
    add_target(db_session, src, c)
    robots()
    rec = Rec()
    r = run(sf, rec)
    row = session_row(db_session, c.id)
    assert row.status == "login_needed" and row.last_error.startswith("login failed")
    blobs = [row.last_error, r.error or "", Path(r.raw_path).read_text(encoding="utf-8"),
             *rec.messages, client.get("/api/credentials").text,
             client.get("/api/custom-sources").text,
             db_session.get(Credential, c.id).last_error or ""]
    for b in blobs:
        assert PASSWORD not in b


@respx.mock
def test_password_echo_redacted_in_raw_and_snapshots(wenv, db_session, monkeypatch):
    src, sf = wenv
    site = FakeSite(monkeypatch)
    site.echo_password = True
    c = mk_login(db_session, status="ok", state=sid_state("s1"))
    add_target(db_session, src, c)
    robots()
    r = run(sf)
    assert r.status == "baseline"
    raw = Path(r.raw_path).read_text(encoding="utf-8")
    db_session.expire_all()
    snaps = [s.content for s in db_session.scalars(select(WatchSnapshot))]
    assert snaps and "v1" in snaps[0]
    for b in [raw, *snaps]:
        assert PASSWORD not in b


@respx.mock
def test_interactive_sign_in_skips_targets_untouched(wenv, db_session, monkeypatch):
    src, sf = wenv
    site = FakeSite(monkeypatch)
    c = mk_login(db_session, status="waiting_code", state=sid_state("s1"))
    add_target(db_session, src, c)
    robots()
    before = session_row(db_session, c.id)
    before = (before.status, bytes(before.state_enc), before.updated_at, before.notified_at)
    rec = Rec()
    r = run(sf, rec)
    assert site.fetches == [] and site.sign_ins == 0
    assert r.status == "failed" and "login_needed: login needed" in r.error
    after = session_row(db_session, c.id)
    assert (after.status, bytes(after.state_enc), after.updated_at, after.notified_at) == before
    assert not any("needs you to sign in" in m for m in rec.messages)


@respx.mock
def test_refreshed_state_not_saved_over_reset_session(wenv, db_session, monkeypatch):
    src, sf = wenv
    site = FakeSite(monkeypatch)
    c = mk_login(db_session, status="ok", state=sid_state("s1"))
    add_target(db_session, src, c)

    def reset(url):  # the user changes the password while the page is being fetched
        with sf() as s:
            s.execute(update(LoginSession).where(LoginSession.credential_id == c.id)
                      .values(status="login_needed", state_enc=None, last_error=None))
            s.commit()

    site.on_fetch = reset
    robots()
    run(sf)
    row = session_row(db_session, c.id)
    assert row.status == "login_needed" and row.state_enc is None


@respx.mock
def test_robots_and_destination_policy_before_browser(wenv, db_session, monkeypatch):
    src, sf = wenv
    site = FakeSite(monkeypatch)
    c = mk_login(db_session, status="ok", state=sid_state("s1"),
                 extra_hosts=["intranet.portal.example"])
    add_target(db_session, src, c, "https://portal.example/a", "A")
    add_target(db_session, src, c, "https://intranet.portal.example/b", "B")
    robots(disallow=True)
    run(sf)
    assert site.fetches == []
    db_session.expire_all()
    ts = {t.kpi_name: t for t in db_session.scalars(select(WatchTarget))}
    assert ts["A"].last_status == "blocked"
    assert (ts["B"].last_status, ts["B"].last_error) == ("error", "destination not allowed")


@respx.mock
def test_target_on_host_no_longer_allowed_not_fetched(wenv, db_session, monkeypatch):
    src, sf = wenv
    site = FakeSite(monkeypatch)
    c = mk_login(db_session, status="ok", state=sid_state("s1"))
    add_target(db_session, src, c, "https://elsewhere.example/a", "A")
    robots()
    run(sf)
    assert site.fetches == []
    db_session.expire_all()
    t = db_session.scalars(select(WatchTarget)).one()
    assert (t.last_status, t.last_error) == ("error", HOST_MSG)


def test_login_page_result_never_becomes_content():
    t = {"id": 1, "kpi_name": "A", "url": "https://portal.example/a", "kind": "page",
         "selector": None}
    e = _process(t, FetchResult("login_needed", error=logins.LOGIN_NEEDED))
    assert e["status"] == "login_needed" and "text" not in e and "json" not in e


# --- Mattermost -------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["ok", "no_change", "baseline", "failed", "suspect_pending"])
def test_alert_appended_for_every_status(status):
    src = Source(id=1, slug="custom-w", name="Watch", area="custom", access="watch")
    r = Run(id=5, source_id=1, status=status, error="boom", rows_total=1, added=1,
            altered=0, removed=0)
    extras = {"page_kpis": [], "excerpt": None, "incomplete_note": None,
              "login_needed": [{"login_name": "Portal", "source": "Watch"}]}
    msg = format_run_message(r, src, [], 1, dashboard_base_url="http://dash/",
                             consecutive_failure_alert=3, extras=extras)
    assert msg.count("needs you to sign in") == 1 and msg.endswith(ALERT)


def test_alert_neutralises_mentions():
    src = Source(id=1, slug="custom-w", name="Watch", area="custom", access="watch")
    r = Run(id=5, source_id=1, status="no_change")
    extras = {"login_needed": [{"login_name": "@channel boss", "source": "Watch"}]}
    msg = format_run_message(r, src, [], 1, dashboard_base_url="http://dash",
                             consecutive_failure_alert=3, extras=extras)
    assert "@channel" not in msg and "@\u200bchannel boss needs you to sign in" in msg


# --- real browser, end to end ------------------------------------------------------------

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


browser = pytest.mark.skipif(not _chromium_available(), reason="Chromium not launchable")


@pytest.fixture
def site(tmp_path, monkeypatch, trust_test_cert):
    from tests.login_site import LoginSite

    monkeypatch.setattr(get_settings(), "watch_allowed_hosts", "127.0.0.1,localhost")
    s = LoginSite(USER, PASSWORD, code_file=tmp_path / "code.txt")
    trust_test_cert(s.spki)
    yield s
    s.close()


@browser
def test_e2e_relogin_then_login_page_never_becomes_content(wenv, db_session, site):
    from tests.login_site import PROTECTED

    src, sf = wenv
    c = mk_login(db_session, login_url=site.url("/login"))
    add_target(db_session, src, c, site.url("/echo"), "Echo")
    add_target(db_session, src, c, site.url("/inline"), "Inline")
    rec = Rec()
    r = run(sf, rec)  # no session yet: signs in by itself, then fetches both pages
    assert r.status == "baseline", r.error
    raw = Path(r.raw_path).read_text(encoding="utf-8")
    assert PROTECTED in raw and PASSWORD not in raw
    db_session.expire_all()
    snaps = [s.content for s in db_session.scalars(select(WatchSnapshot))]
    assert len(snaps) == 2 and all(PROTECTED in s and PASSWORD not in s for s in snaps)
    assert session_row(db_session, c.id).status == "ok"
    # Session dropped by the site and a code is now required: no login page as content.
    site.expire_all()
    site.code_mode = True
    r = run(sf, rec)
    assert r.status == "failed" and "login_needed: login needed" in r.error
    raw = Path(r.raw_path).read_text(encoding="utf-8")
    assert "password" not in raw.lower() and PASSWORD not in raw
    assert all(t["status"] == "login_needed" and "text" not in t
               for t in json.loads(raw)["targets"])
    db_session.expire_all()
    assert len(db_session.scalars(select(WatchSnapshot)).all()) == 2
    assert session_row(db_session, c.id).status == "login_needed"
    assert sum(m.count("needs you to sign in") for m in rec.messages) == 1


@browser
def test_e2e_json_behind_login(wenv, db_session, site):
    from tests.login_site import PROTECTED

    src, sf = wenv
    c = mk_login(db_session, login_url=site.url("/login"))
    t1 = add_target(db_session, src, c, site.url("/api/data"), "Api")  # 401 when signed out
    t1.kind = "api"
    t2 = add_target(db_session, src, c, site.url("/api/json"), "Auto")  # 302 when signed out
    t2.kind = "auto"
    db_session.flush()
    r = run(sf)
    assert r.status == "baseline", r.error
    raw = Path(r.raw_path).read_text(encoding="utf-8")
    assert PASSWORD not in raw
    targets = {x["kpi_name"]: x for x in json.loads(raw)["targets"]}
    for name in ("Api", "Auto"):
        assert targets[name]["kind"] == "api"
        assert targets[name]["json"]["value"] == 42
        assert targets[name]["json"]["label"] == PROTECTED
    db_session.expire_all()
    assert set(db_session.scalars(select(DataPoint.indicator))) == {"Api", "Auto"}
    # Signed out again: the API's 401 counts as an expired session -> one sign-in, data back.
    site.expire_all()
    assert run(sf).status == "no_change"
    assert session_row(db_session, c.id).status == "ok"
