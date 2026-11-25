import base64

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import credentials as creds
from app.api import deps
from app.api.main import create_app
from app.config import get_settings
from app.models import Source, WatchTarget

AUTH = ("anyone", "secret")
SECRET = "sk-super-secret-123"


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
    c.auth = AUTH
    return c


def body(**kw):
    d = {"name": "Acme", "kind": "header", "header_name": "X-Key", "secret": SECRET}
    d.update(kw)
    return d


def test_create_list_never_returns_secret(client):
    r = client.post("/api/credentials", json=body())
    assert r.status_code == 201
    assert SECRET not in r.text
    item = r.json()
    assert item["has_secret"] is True and item["readable"] is True
    assert item["used_by"] == {"targets": [], "sources": []}
    r2 = client.get("/api/credentials")
    assert SECRET not in r2.text and len(r2.json()) == 1
    r3 = client.patch(f"/api/credentials/{item['id']}", json={"secret": "other-secret-xyz"})
    assert r3.status_code == 200 and "other-secret-xyz" not in r3.text


def test_secret_encrypted_at_rest(client, db_session):
    client.post("/api/credentials", json=body())
    raw = db_session.execute(text("SELECT secret_enc FROM credentials")).scalar_one()
    assert SECRET.encode() not in bytes(raw)


@pytest.mark.parametrize("payload", [
    {"kind": "query"},
    {"header_name": "bad name"},
    {"header_name": "x" * 65},
    {"prefix": "a\nb"},
    {"prefix": "p" * 33},
    {"kind": "basic", "username": "a:b"},
    {"secret": "a\nb"},
    {"secret": "x" * 4097},
    {"kind": "basic"},
    {"kind": "bogus"},
    {"secret": ""},
])
def test_kind_field_validation(client, payload):
    assert client.post("/api/credentials", json=body(**payload)).status_code == 422


def test_header_name_defaults_to_authorization(client):
    r = client.post("/api/credentials", json=body(header_name=None))
    assert r.status_code == 201 and r.json()["header_name"] == "Authorization"
    q = client.post("/api/credentials", json=body(name="q", kind="query", param_name="k")).json()
    r = client.patch(f"/api/credentials/{q['id']}", json={"kind": "header"})
    assert r.status_code == 200 and r.json()["header_name"] == "Authorization"
    assert r.json()["param_name"] is None


def test_fields_foreign_to_kind_nulled(client):
    r = client.post("/api/credentials", json=body(
        kind="basic", username="u", header_name="X", prefix="P", param_name="p")).json()
    assert (r["header_name"], r["prefix"], r["param_name"], r["username"]) == (None, None, None, "u")
    r = client.patch(f"/api/credentials/{r['id']}", json={"kind": "query", "param_name": "k"}).json()
    assert (r["username"], r["param_name"]) == (None, "k")


def test_repr_hides_secret():
    c = C(secret="topsecret")
    assert "topsecret" not in repr(c) and "topsecret" not in str(c)


def test_mark_used_keeps_updated_at(client, db_session):
    cid = client.post("/api/credentials", json=body()).json()["id"]
    before = client.get("/api/credentials").json()[0]["updated_at"]
    creds.mark_used(db_session, cid, None)
    after = client.get("/api/credentials").json()[0]
    assert after["updated_at"] == before and after["last_used_at"]


def test_kind_ok_variants(client):
    assert client.post("/api/credentials", json=body(name="q", kind="query", param_name="api_key")).status_code == 201
    assert client.post("/api/credentials", json=body(name="b", kind="basic", username="u")).status_code == 201


def test_patch_rules(client):
    cid = client.post("/api/credentials", json=body()).json()["id"]
    assert client.patch(f"/api/credentials/{cid}", json={"secret": ""}).status_code == 422
    assert client.patch(f"/api/credentials/{cid}", json={"kind": "query"}).status_code == 422
    r = client.patch(f"/api/credentials/{cid}", json={"name": "Renamed"})
    assert r.status_code == 200 and r.json()["name"] == "Renamed"
    assert client.patch("/api/credentials/99999", json={"name": "x"}).status_code == 404


def test_duplicate_name_409(client):
    assert client.post("/api/credentials", json=body(name="Acme")).status_code == 201
    assert client.post("/api/credentials", json=body(name="ACME")).status_code == 409
    other = client.post("/api/credentials", json=body(name="Other")).json()["id"]
    assert client.patch(f"/api/credentials/{other}", json={"name": "acme"}).status_code == 409


def test_missing_secret_key_503(client, monkeypatch):
    monkeypatch.delenv("KPI_SECRET_KEY")
    r = client.post("/api/credentials", json=body())
    assert r.status_code == 503
    assert r.json() == {"detail": "KPI_SECRET_KEY is not set — see README → Keys"}


def test_wrong_secret_key_marks_unreadable(client, monkeypatch):
    client.post("/api/credentials", json=body())
    monkeypatch.setenv("KPI_SECRET_KEY", Fernet.generate_key().decode())
    items = client.get("/api/credentials").json()
    assert items[0]["readable"] is False and items[0]["has_secret"] is True


def test_arbitrary_key_string_is_derived(monkeypatch):
    monkeypatch.setenv("KPI_SECRET_KEY", "just a passphrase")
    assert creds.decrypt(creds.encrypt("x")) == "x"


def test_encrypt_errors(monkeypatch):
    monkeypatch.delenv("KPI_SECRET_KEY")
    with pytest.raises(creds.KeyUnavailable, match="KPI_SECRET_KEY not set"):
        creds.encrypt("x")
    monkeypatch.setenv("KPI_SECRET_KEY", "a")
    blob = creds.encrypt("x")
    monkeypatch.setenv("KPI_SECRET_KEY", "b")
    with pytest.raises(creds.KeyUnavailable, match="cannot decrypt"):
        creds.decrypt(blob)


def _use(db_session):
    src = Source(slug="custom-x", name="X", area="custom", access="watch")
    db_session.add(src)
    db_session.flush()
    t = WatchTarget(source_id=src.id, kpi_name="K", url="https://e.com/a")
    db_session.add(t)
    db_session.flush()
    return src, t


def test_delete_in_use_409(client, db_session):
    cid = client.post("/api/credentials", json=body()).json()["id"]
    src, t = _use(db_session)
    t.credential_id = cid
    src2 = Source(slug="other", name="O", area="a", access="api", credential_id=cid)
    db_session.add(src2)
    db_session.flush()
    used = client.get("/api/credentials").json()[0]["used_by"]
    assert used == {"targets": [{"id": t.id, "kpi_name": "K", "source": "X"}], "sources": ["other"]}
    r = client.delete(f"/api/credentials/{cid}")
    assert r.status_code == 409
    assert r.json() == {"detail": "key is in use", "used_by": used}


def test_delete_unused_204(client):
    cid = client.post("/api/credentials", json=body()).json()["id"]
    assert client.delete(f"/api/credentials/{cid}").status_code == 204
    assert client.get("/api/credentials").json() == []
    assert client.delete(f"/api/credentials/{cid}").status_code == 404


def test_requires_auth(client):
    assert client.get("/api/credentials", auth=None).status_code == 401


def test_load_and_mark_used(client, db_session):
    cid = client.post("/api/credentials", json=body()).json()["id"]
    c = creds.load(db_session, cid)
    assert c.secret == SECRET and c.name == "Acme"
    creds.mark_used(db_session, cid, "boom")
    item = client.get("/api/credentials").json()[0]
    assert item["last_error"] == "boom" and item["last_used_at"]
    creds.mark_used(db_session, cid, None)
    assert client.get("/api/credentials").json()[0]["last_error"] is None


def C(**kw):
    d = dict(id=1, name="n", kind="header", header_name=None, prefix=None,
             param_name=None, username=None, secret="s")
    d.update(kw)
    return creds.Credential(**d)


def test_apply_header_query_basic():
    url, h = creds.apply(C(header_name="Authorization", prefix="Bearer"), "https://e.com/x", {"A": "b"})
    assert url == "https://e.com/x" and h == {"A": "b", "Authorization": "Bearer s"}
    _, h = creds.apply(C(header_name="X-Key"), "https://e.com/x", {})
    assert h == {"X-Key": "s"}
    url, h = creds.apply(C(kind="query", param_name="api_key"), "https://e.com/x?a=1", {})
    assert url == "https://e.com/x?a=1&api_key=s" and h == {}
    url, _ = creds.apply(C(kind="query", param_name="api_key"), "https://e.com/x?api_key=old&a=1", {})
    assert url == "https://e.com/x?a=1&api_key=s"
    url, _ = creds.apply(C(kind="query", param_name="api_key"), "https://e.com/x", {})
    assert url == "https://e.com/x?api_key=s"
    _, h = creds.apply(C(kind="basic", username="u"), "https://e.com/x", {})
    assert h["Authorization"] == "Basic " + base64.b64encode(b"u:s").decode()


def test_redact():
    c = C(secret="s3cr3t")
    assert creds.redact("bad s3cr3t here", c) == "bad *** here"
    b64 = base64.b64encode(b"u:s3cr3t").decode()
    out = creds.redact(f"Authorization: Basic {b64} and s3cr3t", C(kind="basic", username="u", secret="s3cr3t"))
    assert out == "Authorization: Basic *** and ***"
    assert creds.redact("keep", None) == "keep"


def test_redact_url_encoded_forms():
    from urllib.parse import quote, quote_plus
    sec = "a+b/c=d&e f"
    c = C(secret=sec)
    for form in (sec, quote_plus(sec), quote(sec, safe=""), quote(sec)):
        assert creds.redact(f"x {form} y", c) == "x *** y"
