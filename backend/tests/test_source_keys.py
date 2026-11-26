import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from sqlalchemy import func, select

from app import credentials as creds
from app.api import deps
from app.api.main import create_app
from app.config import get_settings
from app.connectors import registry
from app.core import pipeline
from app.models import Credential, Source
from tests.conftest import FakeConnector, api_client

AUTH = ("anyone", "secret")
SECRET = "sk-source-secret"


class KeyedConnector(FakeConnector):
    credential_hosts = ("api.example.com",)
    urls = ("https://api.example.com/data",)

    def download(self, client, dest):
        self.responses = [client.get(u) for u in self.urls]
        dest.write_text("raw")


@pytest.fixture(autouse=True)
def secret_key(monkeypatch):
    monkeypatch.setenv("KPI_SECRET_KEY", Fernet.generate_key().decode())


@pytest.fixture
def env(db_session, session_factory, monkeypatch):
    src = Source(slug="fake", name="Fake", area="test", access="manual")
    db_session.add(src)
    db_session.flush()
    conn = KeyedConnector()
    monkeypatch.setitem(registry.CONNECTORS, "fake", conn)
    return src, conn, session_factory


def mk_cred(db_session, name="Acme", kind="header", secret=SECRET, **kw):
    if kind == "header":
        kw.setdefault("header_name", "X-Key")
    row = Credential(name=name, kind=kind, secret_enc=creds.encrypt(secret), **kw)
    db_session.add(row)
    db_session.flush()
    return row


@pytest.fixture
def client(db_session, session_factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "dashboard_password", "secret")
    app = create_app()
    app.dependency_overrides[deps.get_session] = lambda: db_session
    app.dependency_overrides[deps.get_session_factory] = lambda: session_factory
    c = api_client(app)
    c.auth = AUTH
    return c


@respx.mock
def test_source_key_applied_only_to_declared_https_host(env, db_session):
    src, conn, sf = env
    cred = mk_cred(db_session)
    src.credential_id = cred.id
    db_session.flush()
    conn.urls = ("https://api.example.com/data", "https://other.example.org/data",
                 "http://api.example.com/data")
    respx.get(url__regex=r".*").mock(return_value=httpx.Response(200, text="ok"))
    run = pipeline.run_source(sf, "fake")
    assert run.status == "baseline", run.error
    seen = {str(c.request.url): c.request.headers.get("x-key") for c in respx.calls}
    assert seen == {"https://api.example.com/data": SECRET,
                    "https://other.example.org/data": None,
                    "http://api.example.com/data": None}
    db_session.refresh(cred)
    assert cred.last_used_at is not None and cred.last_error is None


@respx.mock
def test_query_key_and_redirect_to_other_host_drops_key(env, db_session):
    src, conn, sf = env
    cred = mk_cred(db_session, kind="query", header_name=None, param_name="apikey")
    src.credential_id = cred.id
    db_session.flush()
    conn.urls = ("https://api.example.com/a?x=1",)
    respx.get("https://api.example.com/a", params={"x": "1", "apikey": SECRET}).mock(
        return_value=httpx.Response(302, headers={"location": "https://cdn.example.org/f?y=2"}))
    respx.get("https://cdn.example.org/f").mock(return_value=httpx.Response(200, text="ok"))
    run = pipeline.run_source(sf, "fake")
    assert run.status == "baseline", run.error
    assert SECRET not in str(respx.calls[-1].request.url)
    assert SECRET in str(respx.calls[0].request.url)


@respx.mock
def test_query_key_echoed_in_cross_host_location_is_stripped(env, db_session):
    src, conn, sf = env
    secret = "S3cr/et+k"
    cred = mk_cred(db_session, kind="query", header_name=None, param_name="apikey", secret=secret)
    src.credential_id = cred.id
    db_session.flush()
    conn.urls = ("https://api.example.com/a",)
    respx.get("https://api.example.com/a").mock(return_value=httpx.Response(
        302, headers={"location": f"https://cdn.example.org/f?y=2&apikey={secret}"}))
    respx.get(url__startswith="https://cdn.example.org/f").mock(return_value=httpx.Response(200, text="ok"))
    run = pipeline.run_source(sf, "fake")
    assert run.status == "baseline", run.error
    last = str(respx.calls[-1].request.url)
    assert respx.calls[-1].request.url.host == "cdn.example.org"
    assert "apikey" not in last and "S3cr" not in last and "y=2" in last


@respx.mock
def test_source_without_hosts_ignores_key(env, db_session):
    src, conn, sf = env
    conn.credential_hosts = ()
    cred = mk_cred(db_session)
    src.credential_id = cred.id
    db_session.flush()
    respx.get("https://api.example.com/data").mock(return_value=httpx.Response(200, text="ok"))
    run = pipeline.run_source(sf, "fake")
    assert run.status == "baseline"
    assert "x-key" not in respx.calls[0].request.headers
    db_session.refresh(cred)
    assert cred.last_used_at is None


@respx.mock
@pytest.mark.parametrize("code", [401, 403])
def test_source_401_key_rejected(env, db_session, code):
    src, conn, sf = env
    cred = mk_cred(db_session)
    src.credential_id = cred.id
    db_session.flush()
    respx.get("https://api.example.com/data").mock(return_value=httpx.Response(code))
    run = pipeline.run_source(sf, "fake")  # the connector swallows the HTTP error
    assert run.status == "failed"
    assert run.error == f"key rejected (HTTP {code})"
    db_session.refresh(cred)
    assert cred.last_error == f"key rejected (HTTP {code})"


@respx.mock
def test_error_text_is_redacted(env, db_session):
    src, conn, sf = env
    cred = mk_cred(db_session)
    src.credential_id = cred.id
    db_session.flush()

    def boom(client, dest):
        raise RuntimeError(f"bad thing with {SECRET}")

    conn.download = boom
    run = pipeline.run_source(sf, "fake")
    assert run.status == "failed" and SECRET not in run.error and "***" in run.error


def test_key_unavailable_fails_run(env, db_session, monkeypatch):
    src, conn, sf = env
    cred = mk_cred(db_session)
    src.credential_id = cred.id
    db_session.flush()
    monkeypatch.setenv("KPI_SECRET_KEY", "")
    run = pipeline.run_source(sf, "fake")
    assert run.status == "failed"
    assert run.error.startswith("key unavailable (")


@respx.mock
def test_replace_secret_takes_effect_next_run(env, db_session):
    src, conn, sf = env
    cred = mk_cred(db_session, secret="old-secret")
    src.credential_id = cred.id
    db_session.flush()
    respx.get("https://api.example.com/data").mock(return_value=httpx.Response(200, text="ok"))
    pipeline.run_source(sf, "fake")
    cred.secret_enc = creds.encrypt("new-secret")
    db_session.flush()
    pipeline.run_source(sf, "fake")
    assert [c.request.headers["x-key"] for c in respx.calls] == ["old-secret", "new-secret"]


def test_patch_source_credential(env, db_session, client):
    src, conn, sf = env
    cred = mk_cred(db_session)
    r = client.patch("/api/sources/fake", json={"credential_id": cred.id})
    assert r.status_code == 200 and r.json()["credential_id"] == cred.id
    item = next(i for i in client.get("/api/sources").json() if i["slug"] == "fake")
    assert item["credential_id"] == cred.id
    assert item["credential_name"] == "Acme"
    assert item["credential_supported"] is True
    assert SECRET not in client.get("/api/sources").text
    assert client.patch("/api/sources/fake", json={"credential_id": cred.id + 999}).status_code == 422
    assert client.patch("/api/sources/fake", json={"enabled": False}).json()["credential_id"] == cred.id
    assert client.patch("/api/sources/fake", json={"credential_id": None}).status_code == 200
    item = next(i for i in client.get("/api/sources").json() if i["slug"] == "fake")
    assert item["credential_id"] is None and item["credential_name"] is None


def test_credential_supported_false_without_hosts_and_for_watch(env, db_session, client):
    src, conn, sf = env
    conn.credential_hosts = ()
    db_session.add(Source(slug="w", name="W", area="a", access="watch"))
    db_session.flush()
    items = {i["slug"]: i for i in client.get("/api/sources").json()}
    assert items["fake"]["credential_supported"] is False
    assert items["w"]["credential_supported"] is False


def _keyed(db_session, src, **kw):
    cred = mk_cred(db_session, **kw)
    src.credential_id = cred.id
    db_session.flush()
    return cred


@respx.mock
def test_header_key_dropped_on_cross_host_redirect(env, db_session):
    src, conn, sf = env
    _keyed(db_session, src)
    respx.get("https://api.example.com/data").mock(
        return_value=httpx.Response(302, headers={"location": "https://cdn.example.org/f"}))
    respx.get("https://cdn.example.org/f").mock(return_value=httpx.Response(200, text="ok"))
    pipeline.run_source(sf, "fake")
    assert respx.calls[0].request.headers["x-key"] == SECRET
    assert "x-key" not in respx.calls[1].request.headers


@respx.mock
def test_key_dropped_on_https_to_http_same_host(env, db_session):
    src, conn, sf = env
    _keyed(db_session, src)
    respx.get("https://api.example.com/data").mock(
        return_value=httpx.Response(302, headers={"location": "http://api.example.com/f"}))
    respx.get("http://api.example.com/f").mock(return_value=httpx.Response(200, text="ok"))
    pipeline.run_source(sf, "fake")
    assert "x-key" not in respx.calls[1].request.headers


@respx.mock
def test_basic_key_dropped_on_cross_host_redirect(env, db_session):
    src, conn, sf = env
    _keyed(db_session, src, kind="basic", header_name=None, username="u")
    respx.get("https://api.example.com/data").mock(
        return_value=httpx.Response(302, headers={"location": "https://cdn.example.org/f"}))
    respx.get("https://cdn.example.org/f").mock(return_value=httpx.Response(200, text="ok"))
    pipeline.run_source(sf, "fake")
    assert respx.calls[0].request.headers["authorization"].startswith("Basic ")
    assert "authorization" not in respx.calls[1].request.headers


@respx.mock
def test_unkeyed_hop_401_is_not_key_rejected(env, db_session):
    src, conn, sf = env
    cred = _keyed(db_session, src)
    respx.get("https://api.example.com/data").mock(
        return_value=httpx.Response(302, headers={"location": "https://cdn.example.org/f"}))
    respx.get("https://cdn.example.org/f").mock(return_value=httpx.Response(401))
    conn.download = lambda client, dest: (client.get("https://api.example.com/data").raise_for_status(),)
    run = pipeline.run_source(sf, "fake")
    assert run.status == "failed" and "key rejected" not in run.error
    db_session.refresh(cred)
    assert cred.last_error is None


@respx.mock
def test_raw_file_is_redacted(env, db_session):
    from urllib.parse import quote_plus
    src, conn, sf = env
    secret = "p@ss word+/1"
    _keyed(db_session, src, secret=secret)
    respx.get("https://api.example.com/data").mock(return_value=httpx.Response(200, text="ok"))

    def dl(client, dest):
        client.get("https://api.example.com/data")
        dest.write_text(f"a {secret} b {quote_plus(secret)} c")

    conn.download = dl
    run = pipeline.run_source(sf, "fake")
    text = open(run.raw_path).read()
    assert secret not in text and quote_plus(secret) not in text and "***" in text


@respx.mock
def test_query_key_replaces_same_name_param(env, db_session):
    src, conn, sf = env
    _keyed(db_session, src, kind="query", header_name=None, param_name="apikey")
    conn.urls = ("https://api.example.com/a?apikey=stale&x=1",)
    respx.get(url__regex=r".*").mock(return_value=httpx.Response(200, text="ok"))
    pipeline.run_source(sf, "fake")
    assert respx.calls[0].request.url.params.get_list("apikey") == [SECRET]


def test_parse_warning_is_redacted(env, db_session):
    src, conn, sf = env
    _keyed(db_session, src)
    respx.mock.start()
    try:
        respx.get("https://api.example.com/data").mock(return_value=httpx.Response(200, text="ok"))
        conn.warnings = [f"odd value {SECRET}"]
        run = pipeline.run_source(sf, "fake")
    finally:
        respx.mock.stop()
    assert run.error and SECRET not in run.error and "***" in run.error


@respx.mock
def test_unrelated_failure_leaves_key_state_alone(env, db_session):
    src, conn, sf = env
    cred = _keyed(db_session, src)
    cred.last_error = "earlier"
    db_session.flush()
    respx.get("https://api.example.com/data").mock(return_value=httpx.Response(500))
    conn.download = lambda client, dest: client.get("https://api.example.com/data").raise_for_status()
    run = pipeline.run_source(sf, "fake")
    assert run.status == "failed"
    db_session.refresh(cred)
    assert cred.last_error == "earlier" and cred.last_used_at is None


def test_patch_watch_source_credential_422(db_session, client):
    cred = mk_cred(db_session)
    db_session.add(Source(slug="w", name="W", area="a", access="watch"))
    db_session.flush()
    r = client.patch("/api/sources/w", json={"credential_id": cred.id})
    assert r.status_code == 422 and r.json()["detail"] == "this source doesn't use keys"
    assert client.patch("/api/sources/w", json={"credential_id": None}).status_code == 200


def test_raw_file_is_scrubbed_when_download_fails(env, db_session):
    src, conn, sf = env
    _keyed(db_session, src)
    seen = {}

    def dl(client, dest):
        dest.write_text(f"partial {SECRET} data")
        dest.with_name(dest.name + ".part").write_text(f"tmp {SECRET}")
        seen["dest"] = dest
        raise RuntimeError("connection dropped")

    conn.download = dl
    run = pipeline.run_source(sf, "fake")
    assert run.status == "failed" and "connection dropped" in run.error
    dest = seen["dest"]
    assert SECRET not in dest.read_text() and "***" in dest.read_text()
    part = dest.with_name(dest.name + ".part")
    assert SECRET not in part.read_text()


def test_raw_file_is_scrubbed_when_key_rejected(env, db_session):
    src, conn, sf = env
    _keyed(db_session, src)
    respx.mock.start()
    try:
        respx.get("https://api.example.com/data").mock(return_value=httpx.Response(401))

        def dl(client, dest):
            client.get("https://api.example.com/data")
            dest.write_text(f"echo {SECRET}")

        conn.download = dl
        run = pipeline.run_source(sf, "fake")
    finally:
        respx.mock.stop()
    assert run.error == "key rejected (HTTP 401)"
    assert SECRET not in open(run.raw_path).read()


def test_no_open_transaction_during_download(env, db_session, monkeypatch):
    src, conn, sf = env
    _keyed(db_session, src)
    sessions = []
    real = pipeline._load_key

    def spy(session, cid):
        sessions.append(session)
        return real(session, cid)

    monkeypatch.setattr(pipeline, "_load_key", spy)
    during = []

    def dl(client, dest):
        during.append(sessions[0].in_transaction())
        dest.write_text("raw")

    conn.download = dl
    run = pipeline.run_source(sf, "fake")
    assert run.status == "baseline", run.error
    assert during == [False]


LOGIN_PW = "login-password-123"


def mk_login(db_session):
    return mk_cred(db_session, name="Portal", kind="login", secret=LOGIN_PW,
                   username="me@example.com", login_url="https://api.example.com/login")


def test_patch_source_refuses_login_key(env, db_session, client):
    cred = mk_login(db_session)
    r = client.patch("/api/sources/fake", json={"credential_id": cred.id})
    assert r.status_code == 422
    assert r.json()["detail"] == "a login can only be used by KPIs"
    db_session.refresh(env[0])
    assert env[0].credential_id is None


@respx.mock
def test_pipeline_refuses_login_key_on_source(env, db_session):
    """Defence in depth: a login key already attached to a built-in source (older data, a
    direct DB edit) fails the run and never sends the password."""
    src, conn, sf = env
    cred = mk_login(db_session)
    src.credential_id = cred.id
    db_session.flush()
    route = respx.get("https://api.example.com/data").mock(return_value=httpx.Response(200))
    run = pipeline.run_source(sf, "fake")
    assert run.status == "failed"
    assert run.error == "login keys are only used by KPIs"
    assert not route.called


def test_apply_refuses_login_key():
    cred = creds.Credential(1, "Portal", "login", None, None, None, "me", LOGIN_PW,
                            login_url="https://x.example.com/l")
    with pytest.raises(ValueError):
        creds.apply(cred, "https://x.example.com/data", {})


# --- key change: silent baseline and held runs (plan 3a task 4) -------------------

def _row(country, value):
    from app.core.canonical import RawRow
    return RawRow("d", "i", country, "2020", value)


@pytest.fixture
def other_lock(db_engine):
    """Hold the source lock from another connection, as an in-flight run does."""
    from sqlalchemy import text
    held = []

    def hold(source_id):
        c = db_engine.connect()
        params = {"ns": pipeline.LOCK_NAMESPACE, "sid": source_id}
        c.execute(text("SELECT pg_advisory_lock(:ns, :sid)"), params)

        def release():
            c.execute(text("SELECT pg_advisory_unlock(:ns, :sid)"), params)
            c.close()
            held.remove(release)
        held.append(release)
        return release
    yield hold
    for release in list(held):
        release()


@respx.mock
def test_key_change_during_run_keeps_baseline(env, db_session, client, other_lock):
    from app.models import Change, Run
    src, conn, sf = env
    k1, k2 = mk_cred(db_session, name="K1"), mk_cred(db_session, name="K2", secret="sk-other-secret")
    respx.get("https://api.example.com/data").mock(return_value=httpx.Response(200, text="ok"))
    conn.rows = [_row("AT", 1)]
    assert pipeline.run_source(sf, "fake").status == "baseline"
    assert client.patch("/api/sources/fake", json={"credential_id": k1.id}).status_code == 200
    db_session.expire_all()
    assert db_session.get(Source, src.id).needs_baseline is True
    # A run is fetching (it holds the source lock): the key can't change under it, so the
    # run can't finish with the old key and clear the flag the new key needs.
    release = other_lock(src.id)
    r = client.patch("/api/sources/fake", json={"credential_id": k2.id})
    assert r.status_code == 409 and "running" in r.json()["detail"]
    # cron/enabled edits are not blocked by a run
    assert client.patch("/api/sources/fake", json={"enabled": True}).status_code == 200
    release()
    db_session.expire_all()
    assert db_session.get(Source, src.id).credential_id == k1.id
    assert db_session.get(Source, src.id).needs_baseline is True
    # the key unlocks more data, and one value differs: still a silent baseline
    conn.rows = [_row("AT", 2), _row("BE", 3), _row("CZ", 4)]
    run = pipeline.run_source(sf, "fake")
    assert run.status == "baseline", run.error
    assert db_session.scalar(select(func.count()).select_from(Change)) == 0
    db_session.expire_all()
    assert db_session.get(Source, src.id).needs_baseline is False
    # after the baseline, changes alert as usual
    conn.rows = [_row("AT", 5), _row("BE", 3), _row("CZ", 4)]
    run = pipeline.run_source(sf, "fake")
    assert run.status == "ok" and run.altered == 1
    # the same key again is not a change; no key is
    assert client.patch("/api/sources/fake", json={"credential_id": k1.id}).status_code == 200
    db_session.expire_all()
    assert db_session.get(Source, src.id).needs_baseline is False
    assert client.patch("/api/sources/fake", json={"credential_id": None}).status_code == 200
    db_session.expire_all()
    assert db_session.get(Source, src.id).needs_baseline is True
    assert db_session.scalar(select(func.count()).select_from(Run).where(
        Run.source_id == src.id, Run.status == "failed")) == 0


@respx.mock
def test_key_change_supersedes_held_run(env, db_session, client):
    from app.models import PendingChange, Run
    src, conn, sf = env
    k1 = mk_cred(db_session, name="K1")
    respx.get("https://api.example.com/data").mock(return_value=httpx.Response(200, text="ok"))
    conn.rows = [_row("AT", 1), _row("BE", 2)]
    pipeline.run_source(sf, "fake")
    conn.rows = [_row("AT", 9), _row("BE", 2)]
    held = pipeline.run_source(sf, "fake", force_hold=True)
    assert held.status == "suspect_pending"
    assert client.patch("/api/sources/fake", json={"credential_id": k1.id}).status_code == 200
    db_session.expire_all()
    run = db_session.get(Run, held.id)
    assert run.status == "superseded" and "key" in run.error
    assert db_session.scalar(select(func.count()).select_from(PendingChange).where(
        PendingChange.run_id == held.id)) == 0
    assert client.post(f"/api/runs/{held.id}/approve").status_code == 409


def test_key_change_on_source_without_key_hosts_is_not_a_baseline(db_session, client, monkeypatch):
    src = Source(slug="plain", name="Plain", area="test", access="manual")
    db_session.add(src)
    db_session.flush()
    monkeypatch.setitem(registry.CONNECTORS, "plain", FakeConnector())
    k1 = mk_cred(db_session, name="K1")
    assert client.patch("/api/sources/plain", json={"credential_id": k1.id}).status_code == 200
    db_session.expire_all()
    assert db_session.get(Source, src.id).needs_baseline is False


def _dp_count(db_session, src):
    from app.models import DataPoint
    return db_session.scalar(select(func.count()).select_from(DataPoint).where(
        DataPoint.source_id == src.id))


def _n_changes(db_session):
    from app.models import Change
    return db_session.scalar(select(func.count()).select_from(Change))


@respx.mock
def test_incomplete_key_baseline_clears_flag(env, db_session, client):
    src, conn, sf = env
    k1 = mk_cred(db_session, name="K1")
    respx.get("https://api.example.com/data").mock(return_value=httpx.Response(200, text="ok"))
    conn.rows = [_row("AT", 1), _row("BE", 2), _row("CZ", 3)]
    pipeline.run_source(sf, "fake")
    assert client.patch("/api/sources/fake", json={"credential_id": k1.id}).status_code == 200
    conn.rows, conn.complete = [_row("AT", 7), _row("DE", 4)], False
    run = pipeline.run_source(sf, "fake")
    assert run.status == "baseline", run.error
    assert _n_changes(db_session) == 0
    assert _dp_count(db_session, src) == 4  # nothing deleted, DE added
    db_session.expire_all()
    assert db_session.get(Source, src.id).needs_baseline is False
    conn.rows = [_row("AT", 8), _row("BE", 2), _row("CZ", 3), _row("DE", 4)]
    conn.complete = True
    run = pipeline.run_source(sf, "fake")
    assert run.status == "ok" and (run.added, run.altered, run.removed) == (0, 1, 0)


@respx.mock
def test_key_baseline_over_removal_limit_keeps_rows_and_flag(env, db_session, client):
    src, conn, sf = env
    k1 = mk_cred(db_session, name="K1")
    respx.get("https://api.example.com/data").mock(return_value=httpx.Response(200, text="ok"))
    conn.rows = [_row(f"C{i}", i) for i in range(10)]
    pipeline.run_source(sf, "fake")
    assert client.patch("/api/sources/fake", json={"credential_id": k1.id}).status_code == 200
    conn.rows = [_row("C0", 0)]
    run = pipeline.run_source(sf, "fake")
    assert run.status == "baseline"
    assert "key baseline skipped deletions: 9 rows missing (over the safety limit)" in run.error
    assert run.removed == 0  # nothing was removed; the warning carries the count
    assert _dp_count(db_session, src) == 10
    assert _n_changes(db_session) == 0
    db_session.expire_all()
    assert db_session.get(Source, src.id).needs_baseline is True


def test_key_change_on_source_without_key_hosts_takes_no_lock(db_session, client, monkeypatch,
                                                                held_lock):
    src = Source(slug="plain", name="Plain", area="test", access="manual")
    db_session.add(src)
    db_session.flush()
    monkeypatch.setitem(registry.CONNECTORS, "plain", FakeConnector())
    k1 = mk_cred(db_session, name="K1")
    db_session.commit()
    held_lock(src.id)
    assert client.patch("/api/sources/plain", json={"credential_id": k1.id}).status_code == 200


# --- editing a key that is in use (Settings -> Keys) ---------------------------------

BUSY_KEY = "a check is running for a source that uses this key; try again when it finishes"


@respx.mock
def test_secret_replace_on_in_use_key_gives_silent_baseline(env, db_session, client):
    from app.models import Run
    src, conn, sf = env
    k1 = mk_cred(db_session, name="K1")
    src.credential_id = k1.id
    db_session.flush()
    respx.get("https://api.example.com/data").mock(return_value=httpx.Response(200, text="ok"))
    conn.rows = [_row("AT", 1)]
    pipeline.run_source(sf, "fake")
    conn.rows = [_row("AT", 9)]
    held = pipeline.run_source(sf, "fake", force_hold=True)
    assert held.status == "suspect_pending"
    r = client.patch(f"/api/credentials/{k1.id}", json={"secret": "sk-rotated-secret"})
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.get(Source, src.id).needs_baseline is True
    assert db_session.get(Run, held.id).status == "superseded"
    conn.rows = [_row("AT", 2), _row("BE", 3)]
    run = pipeline.run_source(sf, "fake")
    assert run.status == "baseline" and _n_changes(db_session) == 0


def test_key_edit_while_source_runs_409(env, db_session, client, held_lock):
    src, conn, sf = env
    k1 = mk_cred(db_session, name="K1")
    src.credential_id = k1.id
    db_session.commit()
    held_lock(src.id)
    before = db_session.get(Credential, k1.id).secret_enc
    r = client.patch(f"/api/credentials/{k1.id}", json={"secret": "sk-rotated-secret",
                                                         "header_name": "X-Other"})
    assert r.status_code == 409 and r.json()["detail"] == BUSY_KEY
    db_session.expire_all()
    row = db_session.get(Credential, k1.id)
    assert (row.secret_enc, row.header_name) == (before, "X-Key")
    assert db_session.get(Source, src.id).needs_baseline is False


def test_key_rename_takes_no_lock_and_no_baseline(env, db_session, client, held_lock):
    src, conn, sf = env
    k1 = mk_cred(db_session, name="K1")
    src.credential_id = k1.id
    db_session.commit()
    held_lock(src.id)
    assert client.patch(f"/api/credentials/{k1.id}", json={"name": "Renamed"}).status_code == 200
    # unchanged values are not a change either
    assert client.patch(f"/api/credentials/{k1.id}", json={"header_name": "X-Key"}).status_code == 200
    db_session.expire_all()
    assert db_session.get(Source, src.id).needs_baseline is False


@respx.mock
def test_key_baseline_skips_deletions_only_twice(env, db_session, client):
    from app.models import PendingChange
    src, conn, sf = env
    k1 = mk_cred(db_session, name="K1")
    respx.get("https://api.example.com/data").mock(return_value=httpx.Response(200, text="ok"))
    conn.rows = [_row(f"C{i}", i) for i in range(10)]
    pipeline.run_source(sf, "fake")
    assert client.patch("/api/sources/fake", json={"credential_id": k1.id}).status_code == 200
    conn.rows = [_row("C0", 0)]
    flags = []
    runs = []
    for _ in range(2):
        runs.append(pipeline.run_source(sf, "fake"))
        db_session.expire_all()
        flags.append(db_session.get(Source, src.id).needs_baseline)
    assert [r.status for r in runs] == ["baseline", "baseline"]
    assert all("key baseline skipped deletions" in r.error for r in runs)
    assert flags == [True, False]  # a second short fetch is not a reason to stay silent
    assert _n_changes(db_session) == 0 and _dp_count(db_session, src) == 10
    third = pipeline.run_source(sf, "fake")
    assert third.status == "suspect_pending"  # the removal guard holds it for review
    assert _dp_count(db_session, src) == 10
    assert db_session.scalar(select(func.count()).select_from(PendingChange).where(
        PendingChange.run_id == third.id, PendingChange.change_type == "removed")) == 9


def test_key_edit_409_when_a_source_took_the_key_meanwhile(env, db_session, client, monkeypatch):
    from app.api import routes_credentials
    src, conn, sf = env
    k1 = mk_cred(db_session, name="K1")
    db_session.commit()
    before = db_session.get(Credential, k1.id).secret_enc
    calls = iter([[], [src.id]])  # before the locks: unused; after: given to a source
    monkeypatch.setattr(routes_credentials, "_affected_sources", lambda s, cid: next(calls))
    r = client.patch(f"/api/credentials/{k1.id}", json={"secret": "sk-rotated-secret"})
    assert r.status_code == 409 and r.json()["detail"] == BUSY_KEY
    db_session.expire_all()
    assert db_session.get(Credential, k1.id).secret_enc == before


@respx.mock
def test_skip_marker_under_the_previous_key_does_not_count(env, db_session, client):
    """K1's short key baseline left a skip marker; switching to K2 starts the count again:
    K2's first short run keeps the flag, its second clears it."""
    src, conn, sf = env
    k1 = mk_cred(db_session, name="K1")
    k2 = mk_cred(db_session, name="K2", secret="sk-other-secret")
    respx.get("https://api.example.com/data").mock(return_value=httpx.Response(200, text="ok"))
    conn.rows = [_row(f"C{i}", i) for i in range(10)]
    pipeline.run_source(sf, "fake")
    assert client.patch("/api/sources/fake", json={"credential_id": k1.id}).status_code == 200
    conn.rows = [_row("C0", 0)]
    assert "key baseline skipped deletions" in pipeline.run_source(sf, "fake").error  # K1 trips
    assert client.patch("/api/sources/fake", json={"credential_id": k2.id}).status_code == 200
    db_session.expire_all()
    assert db_session.get(Source, src.id).needs_baseline_at is not None
    flags = []
    for _ in range(2):
        run = pipeline.run_source(sf, "fake")
        assert run.status == "baseline" and "key baseline skipped deletions" in run.error
        db_session.expire_all()
        flags.append(db_session.get(Source, src.id).needs_baseline)
    assert flags == [True, False]


def test_key_edit_sets_needs_baseline_at(env, db_session, client):
    src, conn, sf = env
    k1 = mk_cred(db_session, name="K1")
    src.credential_id = k1.id
    db_session.flush()
    assert client.patch(f"/api/credentials/{k1.id}", json={"secret": "sk-rotated-secret"}).status_code == 200
    db_session.expire_all()
    s = db_session.get(Source, src.id)
    assert s.needs_baseline is True and s.needs_baseline_at is not None
