import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import credentials as creds
from app.api import deps
from app.api.main import create_app
from app.config import get_settings

PASSWORD = "hunter2-password"


@pytest.fixture(autouse=True)
def secret_key(monkeypatch):
    monkeypatch.setenv("KPI_SECRET_KEY", Fernet.generate_key().decode())


@pytest.fixture
def client(db_session, session_factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "dashboard_password", "secret")
    app = create_app()
    app.dependency_overrides[deps.get_session] = lambda: db_session
    app.dependency_overrides[deps.get_session_factory] = lambda: session_factory
    c = TestClient(app)
    c.auth = ("anyone", "secret")
    return c


def body(**kw):
    d = {"name": "Portal", "kind": "login", "login_url": "https://Portal.example.com/login",
         "username": "me@example.com", "secret": PASSWORD}
    d.update(kw)
    return d


def test_create_login_key(client, db_session):
    r = client.post("/api/credentials", json=body(
        selectors={"username": "#u", "code": "input.otp"}, extra_hosts=["app.example.com"]))
    assert r.status_code == 201, r.text
    item = r.json()
    assert item["kind"] == "login" and item["username"] == "me@example.com"
    assert item["login_url"] == "https://Portal.example.com/login"
    assert item["selectors"] == {"username": "#u", "code": "input.otp"}
    assert item["extra_hosts"] == ["app.example.com"]
    assert item["header_name"] is None
    row = db_session.execute(text(
        "SELECT status, state_enc, last_error FROM login_sessions WHERE credential_id = :i"),
        {"i": item["id"]}).one()
    assert tuple(row) == ("login_needed", None, None)
    cred = creds.load(db_session, item["id"])
    assert cred.login_url == "https://Portal.example.com/login"
    assert creds.allowed_hosts(cred) == {"portal.example.com", "app.example.com"}


@pytest.mark.parametrize("url", ["http://portal.example.com/login", "portal.example.com/login",
                                 "https:///login", "ftp://x.example.com"])
def test_login_url_must_be_https(client, url):
    r = client.post("/api/credentials", json=body(login_url=url))
    assert r.status_code == 422 and r.json()["detail"] == "login page must be https"
    r = client.post("/api/credentials", json=body(login_url=None))
    assert r.status_code == 422 and r.json()["detail"] == "login page must be https"


def test_login_requires_username(client):
    r = client.post("/api/credentials", json=body(username=None))
    assert r.status_code == 422 and r.json()["detail"] == "username is required"


def test_extra_hosts_validation(client):
    for bad in ["https://a.example.com", "a.example.com:8080", "*.example.com", "A.example.com",
                "a b.example.com", "-a.example.com"]:
        r = client.post("/api/credentials", json=body(extra_hosts=[bad]))
        assert r.status_code == 422 and r.json()["detail"] == f"invalid host: {bad}", bad
    r = client.post("/api/credentials", json=body(extra_hosts=[f"h{i}.example.com" for i in range(11)]))
    assert r.status_code == 422
    r = client.post("/api/credentials", json=body(extra_hosts=[f"h{i}.example.com" for i in range(10)]))
    assert r.status_code == 201


def test_selectors_validation(client):
    r = client.post("/api/credentials", json=body(selectors={"nope": "#x"}))
    assert r.status_code == 422 and r.json()["detail"] == "invalid selector: nope"
    for name, val in [("username", "x" * 201), ("password", "a\nb"), ("submit", "a\rb"),
                      ("code", "a\x00b")]:
        r = client.post("/api/credentials", json=body(selectors={name: val}))
        assert r.status_code == 422 and r.json()["detail"] == f"invalid selector: {name}", name
    r = client.post("/api/credentials", json=body(selectors={"username": "x" * 200}))
    assert r.status_code == 201


def test_login_status_in_list(client, db_session):
    cid = client.post("/api/credentials", json=body()).json()["id"]
    client.post("/api/credentials", json={"name": "Hdr", "kind": "header", "secret": PASSWORD})
    db_session.execute(text(
        "UPDATE login_sessions SET status='failed', last_error='bad password' WHERE credential_id=:i"),
        {"i": cid})
    items = {i["name"]: i for i in client.get("/api/credentials").json()}
    assert items["Portal"]["login_status"] == "failed"
    assert items["Portal"]["login_error"] == "bad password"
    assert "login_status" not in items["Hdr"]


def test_password_never_returned(client):
    r = client.post("/api/credentials", json=body())
    assert PASSWORD not in r.text
    assert PASSWORD not in client.get("/api/credentials").text
    cid = r.json()["id"]
    r = client.patch(f"/api/credentials/{cid}", json={"secret": "another-password-1"})
    assert r.status_code == 200 and "another-password-1" not in r.text


def test_kind_switching(client, db_session):
    cid = client.post("/api/credentials", json=body(extra_hosts=["a.example.com"])).json()["id"]
    r = client.patch(f"/api/credentials/{cid}", json={"kind": "basic"})
    assert r.status_code == 200
    j = r.json()
    assert j["login_url"] is None and j["extra_hosts"] is None and j["username"] == "me@example.com"
    assert "login_status" not in j
    r = client.patch(f"/api/credentials/{cid}", json={"kind": "login"})
    assert r.status_code == 422 and r.json()["detail"] == "login page must be https"
    r = client.patch(f"/api/credentials/{cid}", json={"kind": "login", "login_url": "https://x.example.com/l"})
    assert r.status_code == 200 and r.json()["login_status"] == "login_needed"
    # switching to login on a pre-existing non-login key creates the session row
    other = client.post("/api/credentials", json={"name": "H", "kind": "header", "secret": PASSWORD}).json()["id"]
    r = client.patch(f"/api/credentials/{other}", json={
        "kind": "login", "login_url": "https://y.example.com/l", "username": "u"})
    assert r.status_code == 200 and r.json()["login_status"] == "login_needed"


def test_active_attempt_unique_index(client, db_session):
    cid = client.post("/api/credentials", json=body()).json()["id"]
    ins = "INSERT INTO login_attempts (credential_id, status) VALUES (:i, :s)"
    db_session.execute(text(ins), {"i": cid, "s": "pending"})
    db_session.execute(text(ins), {"i": cid, "s": "failed"})
    with pytest.raises(Exception):
        db_session.execute(text(ins), {"i": cid, "s": "waiting_code"})
