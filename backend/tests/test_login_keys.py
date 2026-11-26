import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import credentials as creds
from app.api import deps
from app.api.main import create_app
from app.config import get_settings
from tests.conftest import api_client

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
    c = api_client(app)
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
    for bad in ["https://a.example.com", "a.example.com:8080", "*.example.com",
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
    with pytest.raises(IntegrityError):
        db_session.execute(text(ins), {"i": cid, "s": "waiting_code"})


def test_patch_resets_session_on_credential_change(client, db_session):
    cid = client.post("/api/credentials", json=body()).json()["id"]

    def mark_ok():
        db_session.execute(text(
            "UPDATE login_sessions SET status='ok', state_enc='\\x01', last_error='e' "
            "WHERE credential_id=:i"), {"i": cid})
        db_session.execute(text("DELETE FROM login_attempts WHERE credential_id=:i"), {"i": cid})
        db_session.execute(text(
            "INSERT INTO login_attempts (credential_id, status, code_enc) VALUES (:i,'waiting_code','\\x02')"),
            {"i": cid})

    def state():
        s = db_session.execute(text(
            "SELECT status, state_enc, last_error FROM login_sessions WHERE credential_id=:i"),
            {"i": cid}).one()
        a = db_session.execute(text(
            "SELECT status, code_enc FROM login_attempts WHERE credential_id=:i"), {"i": cid}).one()
        return tuple(s), tuple(a)

    # unchanged / unrelated edits keep the session
    mark_ok()
    assert client.patch(f"/api/credentials/{cid}", json={"name": "Renamed"}).status_code == 200
    assert client.patch(f"/api/credentials/{cid}", json={"username": "me@example.com"}).status_code == 200
    assert state()[0][0] == "ok"
    for change in ({"username": "other@example.com"}, {"secret": "brand-new-password"},
                   {"login_url": "https://portal.example.com/signin"}):
        mark_ok()
        r = client.patch(f"/api/credentials/{cid}", json=change)
        assert r.status_code == 200 and r.json()["login_status"] == "login_needed"
        db_session.expire_all()
        s, a = state()
        assert s == ("login_needed", None, None), change
        assert a == ("cancelled", None), change


def test_login_url_rejects_userinfo_control_chars_and_length(client):
    for url in ["https://user:pw@portal.example.com/l", "https://portal.example.com/l\nx",
                "https://portal.example.com/ l", "https://portal.example.com/" + "a" * 2048]:
        r = client.post("/api/credentials", json=body(login_url=url))
        assert r.status_code == 422 and r.json()["detail"] == "login page must be https", url


def test_extra_hosts_normalised_and_trailing_newline(client):
    r = client.post("/api/credentials", json=body(extra_hosts=["  App.Example.com "]))
    assert r.status_code == 201 and r.json()["extra_hosts"] == ["app.example.com"]
    r = client.post("/api/credentials", json=body(name="N2", extra_hosts=["a.example.com\n\n"]))
    assert r.status_code == 201 and r.json()["extra_hosts"] == ["a.example.com"]  # stripped
    r = client.post("/api/credentials", json=body(name="N3", extra_hosts=["a.exam\nple.com"]))
    assert r.status_code == 422


def test_colon_in_username_only_banned_for_basic(client):
    assert client.post("/api/credentials", json=body(username="dom:user")).status_code == 201
    r = client.post("/api/credentials", json={
        "name": "B", "kind": "basic", "username": "a:b", "secret": PASSWORD})
    assert r.status_code == 422


def test_delete_cascades_and_restrict_when_in_use(client, db_session):
    from app.models import Source
    cid = client.post("/api/credentials", json=body()).json()["id"]
    db_session.execute(text("INSERT INTO login_attempts (credential_id, status) VALUES (:i,'pending')"),
                       {"i": cid})
    assert client.delete(f"/api/credentials/{cid}").status_code == 204
    for t in ("login_sessions", "login_attempts"):
        assert db_session.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() == 0
    cid = client.post("/api/credentials", json=body(name="Used")).json()["id"]
    db_session.add(Source(slug="s-login", name="S", area="a", access="public", credential_id=cid))
    db_session.flush()
    assert client.delete(f"/api/credentials/{cid}").status_code == 409
    assert db_session.execute(text("SELECT count(*) FROM login_sessions")).scalar_one() == 1


def test_downgrade_refused_with_login_keys(db_engine):
    import subprocess
    import sys
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    base = make_url(db_engine.url.render_as_string(hide_password=False))
    name = f"{base.database}_dg"  # per test DB, so parallel test DBs never share it
    url = base.set(database=name)
    with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        c.execute(text(f"CREATE DATABASE {name}"))
    env = {**__import__("os").environ, "DATABASE_URL": url.render_as_string(hide_password=False)}

    def alembic(*args):
        return subprocess.run([sys.executable, "-m", "alembic", *args], env=env, cwd=str(
            __import__("pathlib").Path(__file__).resolve().parent.parent),
            capture_output=True, text=True)
    try:
        assert alembic("upgrade", "head").returncode == 0
        e = create_engine(url)
        with e.begin() as c:
            c.execute(text("INSERT INTO credentials (name, kind, secret_enc, login_url, username) "
                           "VALUES ('x','login','\\x01','https://a.example.com/','u')"))
        with e.connect() as c:
            head = c.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        r = alembic("downgrade", "0007")
        assert r.returncode != 0 and "delete login keys before downgrading 0008" in r.stderr
        with e.connect() as c:
            assert c.execute(text("SELECT count(*) FROM credentials")).scalar_one() == 1
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == head
            assert c.execute(text("SELECT to_regclass('login_sessions')")).scalar_one() is not None
            assert c.execute(text(
                "SELECT 1 FROM information_schema.columns WHERE table_name = 'sources' "
                "AND column_name = 'needs_baseline'")).first() is not None
        with e.begin() as c:
            c.execute(text("DELETE FROM credentials"))
        assert alembic("downgrade", "0007").returncode == 0
        e.dispose()
    finally:
        with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))


def test_migration_0009_downgrade_and_upgrade(db_engine):
    import subprocess
    import sys
    from pathlib import Path
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    base = make_url(db_engine.url.render_as_string(hide_password=False))
    name = f"{base.database}_m9"
    url = base.set(database=name)
    with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        c.execute(text(f"CREATE DATABASE {name}"))
    env = {**__import__("os").environ, "DATABASE_URL": url.render_as_string(hide_password=False)}

    def alembic(*args):
        r = subprocess.run([sys.executable, "-m", "alembic", *args], env=env,
                           cwd=str(Path(__file__).resolve().parent.parent),
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    has_col = text("SELECT 1 FROM information_schema.columns WHERE table_name = 'sources' "
                   "AND column_name = 'needs_baseline'")
    e = create_engine(url)
    try:
        alembic("upgrade", "0009")
        with e.begin() as c:
            assert c.execute(has_col).first() is not None
            c.execute(text("INSERT INTO sources (slug, name, area, access, needs_baseline) "
                           "VALUES ('s', 'S', 'a', 'manual', true)"))
        alembic("downgrade", "0008")
        with e.connect() as c:
            assert c.execute(has_col).first() is None
            assert c.execute(text("SELECT count(*) FROM sources")).scalar_one() == 1
        alembic("upgrade", "head")
        with e.connect() as c:
            assert c.execute(has_col).first() is not None
            assert c.execute(text("SELECT needs_baseline FROM sources")).scalar_one() is False
    finally:
        e.dispose()
        with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
