import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import credentials as creds
from app.api import deps
from app.api.main import create_app
from app.config import get_settings
from app.connectors import registry
from app.core import pipeline
from app.models import Credential, Source
from tests.conftest import FakeConnector

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
    c = TestClient(app)
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
