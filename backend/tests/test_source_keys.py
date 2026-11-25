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
