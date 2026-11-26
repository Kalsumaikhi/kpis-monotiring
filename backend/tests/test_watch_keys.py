import base64
import io
import json
import logging
import re

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from sqlalchemy import select

from app import credentials as creds
from app.api import deps
from app.api.main import create_app
from app.config import get_settings
from app.connectors.watch import fetch as fetch_mod
from app.connectors.watch.fetch import fetch_target
from app.core import pipeline
from app.models import Credential, Run, Source, WatchSnapshot, WatchTarget
from tests.conftest import api_client

AUTH = ("anyone", "secret")
SECRET = "sk-super-secret+/123"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("KPI_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(fetch_mod, "MIN_INTERVAL", 0.0)
    real = fetch_mod.socket.getaddrinfo

    def fake(host, *a, **k):
        if host in ("localhost", "127.0.0.1"):
            return real(host, *a, **k)
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(fetch_mod.socket, "getaddrinfo", fake)


@pytest.fixture
def client(db_session, session_factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "dashboard_password", "secret")
    app = create_app()
    app.dependency_overrides[deps.get_session] = lambda: db_session
    app.dependency_overrides[deps.get_session_factory] = lambda: session_factory
    c = api_client(app)
    c.auth = AUTH
    return c


def mk_cred(db_session, name="Acme", kind="header", secret=SECRET, **kw):
    if kind == "header":
        kw.setdefault("header_name", "X-Key")
    row = Credential(name=name, kind=kind, secret_enc=creds.encrypt(secret), **kw)
    db_session.add(row)
    db_session.flush()
    return row


def cred_obj(kind="header", **kw):
    d = dict(id=1, name="n", kind=kind, header_name=None, prefix=None, param_name=None,
             username=None, secret=SECRET)
    d.update(kw)
    return creds.Credential(**d)


def robots():
    respx.get(url__regex=r"https://[a-z.]+/robots\.txt").mock(return_value=httpx.Response(404))


def fetch(url, cred, **kw):
    with httpx.Client() as c:
        return fetch_target(c, url, state=fetch_mod.FetchState(), credential=cred, **kw)


# --- fetch_target ---------------------------------------------------------------

@respx.mock
def test_header_key_sent_to_same_host():
    robots()
    r = respx.get("https://a.com/x").mock(return_value=httpx.Response(200, text="ok"))
    res = fetch("https://a.com/x", cred_obj(header_name="X-Key", prefix="Token"))
    assert res.status == "ok"
    assert r.calls[0].request.headers["X-Key"] == f"Token {SECRET}"
    for call in respx.calls:  # robots.txt never gets the key
        if call.request.url.path == "/robots.txt":
            assert "X-Key" not in call.request.headers


@respx.mock
def test_query_key_appended():
    robots()
    r = respx.get("https://a.com/x").mock(return_value=httpx.Response(200, text="ok"))
    assert fetch("https://a.com/x?a=1", cred_obj("query", param_name="api_key")).status == "ok"
    q = dict(r.calls[0].request.url.params)
    assert q == {"a": "1", "api_key": SECRET}


@respx.mock
def test_basic_key():
    robots()
    r = respx.get("https://a.com/x").mock(return_value=httpx.Response(200, text="ok"))
    fetch("https://a.com/x", cred_obj("basic", username="bob"))
    want = "Basic " + base64.b64encode(f"bob:{SECRET}".encode()).decode()
    assert r.calls[0].request.headers["Authorization"] == want


@respx.mock
def test_key_dropped_on_cross_host_redirect():
    robots()
    respx.get("https://a.com/x").mock(
        return_value=httpx.Response(302, headers={"location": "https://b.com/y?z=1"}))
    b = respx.get("https://b.com/y").mock(return_value=httpx.Response(200, text="ok"))
    for cred in (cred_obj("header", header_name="Authorization"),
                 cred_obj("query", param_name="api_key")):
        b.calls.reset()
        assert fetch("https://a.com/x", cred).status == "ok"
        req = b.calls[0].request
        assert "Authorization" not in req.headers and "api_key" not in req.url.params


ECHO = "S3cr/et+k"


@respx.mock
@pytest.mark.parametrize("raw", ["S3cr/et+k", "S3cr%2Fet%2Bk", "S3cr%2fet%2bk"])
def test_query_key_echoed_in_cross_host_location_is_stripped(raw):
    robots()
    respx.get("https://a.com/x").mock(return_value=httpx.Response(
        302, headers={"location": f"https://b.com/x?z=1&api_key={raw}"}))
    b = respx.get(url__startswith="https://b.com/x").mock(return_value=httpx.Response(200, text="ok"))
    res = fetch("https://a.com/x", cred_obj("query", param_name="api_key", secret=ECHO))
    assert res.status == "ok"
    sent = str(b.calls[0].request.url)
    assert "api_key" not in sent and "z=1" in sent
    for call in respx.calls:
        if call.request.url.host != "a.com":
            assert ECHO not in str(call.request.url) and "S3cr" not in str(call.request.url)


@respx.mock
def test_query_key_echoed_in_https_to_http_same_host_location_is_stripped():
    robots()
    respx.get("https://a.com/x").mock(return_value=httpx.Response(
        302, headers={"location": f"http://a.com/y?api_key={ECHO}"}))
    respx.get("http://a.com/robots.txt").mock(return_value=httpx.Response(404))
    y = respx.get(url__startswith="http://a.com/y").mock(return_value=httpx.Response(200, text="ok"))
    assert fetch("https://a.com/x", cred_obj("query", param_name="api_key", secret=ECHO)).status == "ok"
    for call in respx.calls:
        if call.request.url.scheme == "http":
            assert "S3cr" not in str(call.request.url)
    assert "api_key" not in str(y.calls[0].request.url)


@respx.mock
def test_query_key_echoed_on_same_host_https_hop_sent_once():
    robots()
    respx.get("https://a.com/x").mock(return_value=httpx.Response(
        302, headers={"location": f"https://a.com/y?api_key={ECHO}"}))
    y = respx.get(url__startswith="https://a.com/y").mock(return_value=httpx.Response(200, text="ok"))
    assert fetch("https://a.com/x", cred_obj("query", param_name="api_key", secret=ECHO)).status == "ok"
    assert y.calls[0].request.url.params.get_list("api_key") == [ECHO]


def test_strip_query_secret_leaves_other_params_alone():
    c = cred_obj("query", param_name="api_key", secret=ECHO)
    assert creds.strip_query_secret(c, "https://b.com/x?a=%2F&api_key=S3cr/et+k&b=2") == \
        "https://b.com/x?a=%2F&b=2"
    assert creds.strip_query_secret(c, "https://b.com/x?api_key=other") == "https://b.com/x?api_key=other"
    assert creds.strip_query_secret(cred_obj(), "https://b.com/x?api_key=S3cr/et+k") == \
        "https://b.com/x?api_key=S3cr/et+k"


@respx.mock
def test_key_kept_on_same_host_redirect():
    robots()
    respx.get("https://a.com/x").mock(
        return_value=httpx.Response(302, headers={"location": "/y"}))
    y = respx.get("https://a.com/y").mock(return_value=httpx.Response(200, text="ok"))
    assert fetch("https://a.com/x", cred_obj(header_name="X-Key")).status == "ok"
    assert y.calls[0].request.headers["X-Key"] == SECRET


@respx.mock
def test_key_not_sent_on_https_to_http_downgrade_redirect():
    robots()
    respx.get("https://a.com/x").mock(
        return_value=httpx.Response(302, headers={"location": "http://a.com/y"}))
    respx.get("http://a.com/robots.txt").mock(return_value=httpx.Response(404))
    y = respx.get("http://a.com/y").mock(return_value=httpx.Response(200, text="ok"))
    fetch("https://a.com/x", cred_obj(header_name="X-Key"))
    assert "X-Key" not in y.calls[0].request.headers


def test_http_and_browser_refused_in_fetch():
    r = fetch("http://a.com/x", cred_obj())
    assert (r.status, r.error) == ("error", "keys can only be sent over https")
    r = fetch("https://a.com/x", cred_obj(), use_browser=True)
    assert (r.status, r.error) == ("error", 'keys can\'t be used with "Needs a browser" yet')


@respx.mock
@pytest.mark.parametrize("code", [401, 403])
def test_rejected_status(code):
    robots()
    respx.get("https://a.com/x").mock(return_value=httpx.Response(code))
    res = fetch("https://a.com/x", cred_obj())
    assert (res.status, res.error) == ("key_rejected", f"key rejected (HTTP {code})")


@respx.mock
def test_401_without_key_is_plain_error():
    robots()
    respx.get("https://a.com/x").mock(return_value=httpx.Response(401))
    res = fetch("https://a.com/x", None)
    assert (res.status, res.error) == ("error", "HTTP 401")


@respx.mock
def test_response_body_echoing_key_is_scrubbed():
    robots()
    respx.get("https://a.com/x").mock(
        return_value=httpx.Response(200, text=f"<p>you sent {SECRET}</p>"))
    res = fetch("https://a.com/x", cred_obj())
    assert SECRET.encode() not in res.body and b"***" in res.body


@respx.mock
def test_json_escaped_echo_is_scrubbed():
    robots()
    respx.get("https://a.com/x").mock(return_value=httpx.Response(
        200, content=b'{"echo":"S3cr\\/et+k","u":"S3cr\\u002fet+k"}',
        headers={"content-type": "application/json"}))
    c = cred_obj(secret="S3cr/et+k")
    res = fetch("https://a.com/x", c)
    assert b"S3cr" not in res.body.replace(b"S3cr\\u002fet+k", b"") and b"***" in res.body
    from app.connectors.watch.connector import _process
    out = _process({"id": 1, "kpi_name": "K", "url": "u", "kind": "api", "selector": None},
                   res, c)
    assert out["status"] == "ok" and "S3cr" not in json.dumps(out) and out["json"]["echo"] == "***"
    assert out["json"]["u"] == "***"


def test_redact_json_escaped_forms():
    c = cred_obj(secret='a/b"c\\d')
    assert creds.redact(json.dumps({"k": 'a/b"c\\d'}), c) == '{"k": "***"}'
    assert creds.redact('{"k": "a\\/b\\"c\\\\d"}', c) == '{"k": "***"}'
    assert creds.redact_bytes(b'{"k":"a\\/b\\"c\\\\d"}', c) == b'{"k":"***"}'


@respx.mock
def test_keyed_request_not_logged(caplog):
    robots()
    respx.get(url__startswith="https://a.com/x").mock(return_value=httpx.Response(200, text="ok"))
    caplog.set_level(logging.INFO)
    fetch("https://a.com/x", cred_obj("query", param_name="api_key", secret="LOGSECRET9"))
    assert "LOGSECRET9" not in caplog.text


# --- redact ---------------------------------------------------------------------

def test_redact_percent_case_insensitive_and_basic_url_encoded():
    from urllib.parse import quote
    c = cred_obj(secret="a+b/c=")
    assert creds.redact("k=a%2Bb%2Fc%3D", c) == "k=***"
    assert creds.redact("k=a%2bb%2fc%3d", c) == "k=***"
    b = cred_obj("basic", username="u", secret="pw")
    tok = base64.b64encode(b"u:pw").decode()
    assert creds.redact(f"x {quote(tok, safe='')} y", b) == "x *** y"
    low = re.sub(r"%[0-9A-F]{2}", lambda m: m.group().lower(), quote(tok, safe=""))
    assert low != quote(tok, safe="") and creds.redact(f"x {low} y", b) == "x *** y"
    assert creds.redact("PW stays", c) == "PW stays"


# --- API ------------------------------------------------------------------------

def create(client, **extra):
    return client.post("/api/custom-sources", json={
        "source_name": "Acme", "kpi_name": "Price", "url": "https://h.example/p", **extra})


def test_create_with_key_returns_name_and_no_secret(client, db_session):
    c = mk_cred(db_session)
    r = create(client, credential_id=c.id)
    assert r.status_code == 201
    assert r.json()["credential_id"] == c.id and r.json()["credential_name"] == "Acme"
    assert SECRET not in r.text
    lst = client.get("/api/custom-sources").json()
    assert lst[0]["targets"][0]["credential_name"] == "Acme"
    assert create(client, kpi_name="B", url="https://h.example/q").json()["credential_id"] is None


def test_unknown_credential_422(client):
    assert create(client, credential_id=99999).status_code == 422


def test_http_url_with_key_422(client, db_session):
    c = mk_cred(db_session)
    r = create(client, url="http://h.example/p", credential_id=c.id)
    assert r.status_code == 422 and r.json()["detail"] == "keys can only be sent over https"
    ok = create(client, credential_id=c.id).json()
    r = client.patch(f"/api/watch-targets/{ok['id']}", json={"url": "http://h.example/q"})
    assert r.status_code == 422 and r.json()["detail"] == "keys can only be sent over https"
    plain = create(client, kpi_name="P", url="http://h.example/z").json()
    r = client.patch(f"/api/watch-targets/{plain['id']}", json={"credential_id": c.id})
    assert r.status_code == 422 and r.json()["detail"] == "keys can only be sent over https"


def test_browser_with_key_422(client, db_session):
    c = mk_cred(db_session)
    msg = 'keys can\'t be used with "Needs a browser" yet'
    r = create(client, credential_id=c.id, use_browser=True)
    assert r.status_code == 422 and r.json()["detail"] == msg
    ok = create(client, credential_id=c.id).json()
    r = client.patch(f"/api/watch-targets/{ok['id']}", json={"use_browser": True})
    assert r.status_code == 422 and r.json()["detail"] == msg


def test_patch_sets_and_clears_key(client, db_session):
    c = mk_cred(db_session)
    t = create(client).json()
    r = client.patch(f"/api/watch-targets/{t['id']}", json={"credential_id": c.id})
    assert r.json()["credential_id"] == c.id and r.json()["credential_name"] == "Acme"
    r = client.patch(f"/api/watch-targets/{t['id']}", json={"credential_id": None})
    assert r.json()["credential_id"] is None and r.json()["credential_name"] is None


@respx.mock
def test_test_link_uses_key(client, db_session):
    c = mk_cred(db_session, kind="query", param_name="api_key", header_name=None)
    robots()
    r = respx.get("https://h.example/p").mock(
        return_value=httpx.Response(200, json={"v": 1}, headers={"content-type": "application/json"}))
    out = client.post("/api/watch-targets/test",
                      json={"url": "https://h.example/p", "credential_id": c.id})
    assert out.json()["status"] == "ok"
    assert r.calls[0].request.url.params["api_key"] == SECRET
    assert SECRET not in out.text
    bad = client.post("/api/watch-targets/test",
                      json={"url": "http://h.example/p", "credential_id": c.id})
    assert bad.status_code == 422


@respx.mock
def test_test_link_key_rejected(client, db_session):
    c = mk_cred(db_session)
    robots()
    respx.get("https://h.example/p").mock(return_value=httpx.Response(403))
    out = client.post("/api/watch-targets/test",
                      json={"url": "https://h.example/p", "credential_id": c.id}).json()
    assert out["status"] == "key_rejected" and out["error"] == "key rejected (HTTP 403)"


def sheet(rows, header="Source name,KPI name,Link,Key  NAME"):
    return {"file": ("s.csv", io.BytesIO((header + "\n" + "\n".join(rows) + "\n").encode()))}


def test_import_key_name_column(client, db_session):
    c = mk_cred(db_session, name="Acme Key")
    rows = ["A,K1,https://x.example/1,acme key", "A,K2,https://x.example/2,nope",
            "A,K3,http://x.example/3,Acme Key", "A,K4,https://x.example/4,"]
    p = client.post("/api/watch-targets/import/preview", files=sheet(rows)).json()
    by = {r["kpi_name"]: r for r in p["rows"]}
    assert by["K1"]["status"] == "new_source" and by["K1"]["credential_id"] == c.id
    assert by["K2"]["status"] == "invalid" and by["K2"]["error"] == "unknown key: nope"
    assert by["K3"]["status"] == "invalid" and by["K3"]["error"] == "keys can only be sent over https"
    assert by["K4"]["status"] == "existing_source" or by["K4"]["status"] == "new_source"
    client.post("/api/watch-targets/import/confirm", json={"token": p["token"]})
    ts = {t.kpi_name: t for t in db_session.scalars(select(WatchTarget))}
    assert ts["K1"].credential_id == c.id and ts["K4"].credential_id is None
    assert "K2" not in ts and "K3" not in ts


def test_import_without_key_column_still_works(client):
    r = client.post("/api/watch-targets/import/preview",
                    files=sheet(["A,K1,https://x.example/1"], header="Source name,KPI name,Link"))
    assert r.json()["rows"][0]["status"] == "new_source"


# --- connector / pipeline -----------------------------------------------------------

@pytest.fixture
def wenv(db_session, session_factory):
    src = Source(slug="custom-x", name="X", area="custom", access="watch")
    db_session.add(src)
    db_session.flush()
    return src, session_factory


def add_target(db_session, src, cred, url="https://h.example/p", name="Price"):
    t = WatchTarget(source_id=src.id, kpi_name=name, url=url, kind="page",
                    credential_id=cred.id if cred else None)
    db_session.add(t)
    db_session.flush()
    return t


@respx.mock
def test_query_key_not_persisted_anywhere(wenv, db_session, client):
    src, sf = wenv
    c = mk_cred(db_session, kind="query", param_name="api_key", header_name=None)
    t = add_target(db_session, src, c)
    robots()
    respx.get(url__startswith="https://h.example/p").mock(
        return_value=httpx.Response(200, text="<main>v1</main>", headers={"content-type": "text/html"}))
    sent = []

    class Rec:
        def notify_run(self, run, source, top, streak, extras=None):
            sent.append(json.dumps([run.error, extras], default=str))

    run = pipeline.run_source(sf, "custom-x", notifier=Rec())
    assert any("api_key=" in str(call.request.url) and SECRET.replace("+", "%2B").replace("/", "%2F")
               in str(call.request.url) for call in respx.calls)
    raw = open(run.raw_path, encoding="utf-8").read()
    data = json.loads(raw)["targets"][0]
    assert data["url"] == "https://h.example/p" and data["credential_id"] == c.id
    blobs = [raw, run.error or "", *sent]
    db_session.expire_all()
    blobs += [s.content for s in db_session.scalars(select(WatchSnapshot))]
    tt = db_session.get(WatchTarget, t.id)
    blobs += [tt.last_error or "", tt.url]
    blobs.append(client.get("/api/custom-sources").text)
    blobs.append(client.get("/api/credentials").text)
    for b in blobs:
        for form in (SECRET, SECRET.replace("+", "%2B").replace("/", "%2F")):
            assert form not in b
    assert db_session.get(Credential, c.id).last_used_at is not None


@respx.mock
def test_pipeline_401_key_rejected_and_incomplete_warning(wenv, db_session):
    src, sf = wenv
    c = mk_cred(db_session)
    add_target(db_session, src, c)
    add_target(db_session, src, None, url="https://h.example/o", name="Other")
    robots()
    respx.get("https://h.example/p").mock(return_value=httpx.Response(401))
    respx.get("https://h.example/o").mock(
        return_value=httpx.Response(200, text="<main>a</main>", headers={"content-type": "text/html"}))
    run = pipeline.run_source(sf, "custom-x")
    assert run.error.startswith(
        "incomplete: 1 KPI(s) not checked: Price (key_rejected: key rejected (HTTP 401))")
    db_session.expire_all()
    tgt = db_session.scalars(select(WatchTarget).where(WatchTarget.kpi_name == "Price")).one()
    assert tgt.last_status == "key_rejected" and tgt.last_error == "key rejected (HTTP 401)"
    assert db_session.get(Credential, c.id).last_error == "key rejected (HTTP 401)"


@respx.mock
def test_key_unavailable_target_error(wenv, db_session, monkeypatch):
    src, sf = wenv
    c = mk_cred(db_session)
    add_target(db_session, src, c)
    add_target(db_session, src, None, url="https://h.example/o", name="Other")
    robots()
    p = respx.get("https://h.example/p").mock(return_value=httpx.Response(200, text="x"))
    respx.get("https://h.example/o").mock(
        return_value=httpx.Response(200, text="<main>a</main>", headers={"content-type": "text/html"}))
    monkeypatch.setenv("KPI_SECRET_KEY", "")
    run = pipeline.run_source(sf, "custom-x")
    assert "Price (error: key unavailable (KPI_SECRET_KEY not set))" in run.error
    assert not p.called


@respx.mock
def test_all_targets_rejected_names_key_in_run_error_and_alert(wenv, db_session):
    from app.notify.mattermost import format_run_message
    src, sf = wenv
    c = mk_cred(db_session)
    add_target(db_session, src, c)
    robots()
    respx.get("https://h.example/p").mock(return_value=httpx.Response(401))
    sent = []

    class Rec:
        def notify_run(self, run, source, top, streak, extras=None):
            sent.append(format_run_message(run, source, top, streak, dashboard_base_url="http://d",
                                           consecutive_failure_alert=3, extras=extras))

    run = pipeline.run_source(sf, "custom-x", notifier=Rec())
    assert len(sent) == 1 and "check failed:" in sent[0]
    assert "key rejected (HTTP 401)" in sent[0] and SECRET not in sent[0]
    assert run.status == "failed"
    assert "all targets failed: Price (key_rejected: key rejected (HTTP 401))" in run.error
    assert SECRET not in run.error
    db_session.expire_all()
    assert db_session.get(Credential, c.id).last_error == "key rejected (HTTP 401)"


@respx.mock
def test_changing_key_rebaselines_without_alert(wenv, db_session, client):
    src, sf = wenv
    c = mk_cred(db_session)
    t = add_target(db_session, src, None)
    robots()
    route = respx.get("https://h.example/p").mock(return_value=httpx.Response(
        200, text="<main>teaser</main>", headers={"content-type": "text/html"}))
    sent = []

    class Rec:
        def notify_run(self, run, source, top, streak, extras=None):
            sent.append((run.status, run.added + run.altered + run.removed, extras))

    assert pipeline.run_source(sf, "custom-x", notifier=Rec()).status == "baseline"
    db_session.expire_all()
    assert db_session.get(WatchTarget, t.id).needs_baseline is False
    r = client.patch(f"/api/watch-targets/{t.id}", json={"credential_id": c.id})
    assert r.status_code == 200
    db_session.expire_all()
    assert db_session.get(WatchTarget, t.id).needs_baseline is True
    route.mock(return_value=httpx.Response(
        200, text="<main>full private page</main>", headers={"content-type": "text/html"}))
    sent.clear()
    run = pipeline.run_source(sf, "custom-x", notifier=Rec())
    assert route.calls[-1].request.headers["X-Key"] == SECRET
    assert run.added + run.altered + run.removed == 0
    assert not any((extras or {}).get("page_kpis") for _, _, extras in sent)
    db_session.expire_all()
    assert db_session.get(WatchTarget, t.id).needs_baseline is False
    # an unchanged key in a PATCH is not a change; clearing it is
    client.patch(f"/api/watch-targets/{t.id}", json={"credential_id": c.id})
    db_session.expire_all()
    assert db_session.get(WatchTarget, t.id).needs_baseline is False
    client.patch(f"/api/watch-targets/{t.id}", json={"credential_id": None})
    db_session.expire_all()
    assert db_session.get(WatchTarget, t.id).needs_baseline is True


def test_key_changed_during_run_keeps_needs_baseline(wenv, db_session, tmp_path):
    from app.connectors.watch.connector import WatchConnector
    src, sf = wenv
    c = mk_cred(db_session)
    t = add_target(db_session, src, c)
    t.needs_baseline = True
    raw = tmp_path / "r.json"
    raw.write_text(json.dumps({"targets": [{
        "id": t.id, "kpi_name": "Price", "url": t.url, "kind": "page", "credential_id": None,
        "needs_baseline": True, "status": "ok", "content_type": "text/html", "error": None,
        "text": "x", "sha256": "0" * 64}]}))
    run = Run(source_id=src.id, status="baseline", trigger="manual", raw_path=str(raw))
    db_session.add(run)
    db_session.flush()
    WatchConnector(src.id, "custom-x", "X", sf).after_run(db_session, run)
    db_session.expire_all()
    assert db_session.get(WatchTarget, t.id).needs_baseline is True
