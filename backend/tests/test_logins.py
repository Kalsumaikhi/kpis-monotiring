"""Login engine (app.logins) against a real Chromium and the local HTTPS login site.

The site (tests/login_site.py) is served over https with a throwaway self-signed certificate;
Chromium trusts exactly that key through the test-only KPI_TEST_TRUSTED_SPKI. `localhost` is
the login host; `127.0.0.1` is the same server under a second host name, allowed by the
destination policy but NOT by the login (unless a test adds it to extra_hosts).
"""

import json
import logging
import os
import time

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from app import credentials as creds
from app import logins
from app.config import get_settings
from app.connectors.watch import fetch as fetch_mod
from app.models import Credential as CredentialRow
from tests.login_site import PROTECTED, LoginSite

USER = "me@example.com"
PASSWORD = "Sup3r-secret-pass!"


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            exe = os.environ.get("KPI_CHROMIUM_PATH")
            b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
            b.close()
        return True
    except Exception:  # noqa: BLE001
        return False


browser = pytest.mark.skipif(not _chromium_available(), reason="Chromium not launchable")


@pytest.fixture(autouse=True)
def secret_key(monkeypatch):
    monkeypatch.setenv("KPI_SECRET_KEY", Fernet.generate_key().decode())


@pytest.fixture
def site(tmp_path, monkeypatch, trust_test_cert):
    monkeypatch.setattr(get_settings(), "watch_allowed_hosts", "127.0.0.1,localhost")
    s = LoginSite(USER, PASSWORD, code_file=tmp_path / "code.txt")
    trust_test_cert(s.spki)
    yield s
    s.close()


def cred_for(site, path="/login", selectors=None, extra_hosts=None, cid=1):
    return creds.Credential(cid, "Portal", "login", None, None, None, USER, PASSWORD,
                            login_url=site.url(path), selectors=selectors,
                            extra_hosts=extra_hosts)


def code_from(site):
    def provider():
        f = site.code_file
        return f.read_text() if f.exists() else None
    return provider


def cookie_hosts(state):
    return {c["domain"].lstrip(".") for c in state["cookies"]}


# --- pure helpers -------------------------------------------------------------------------

def test_filter_state_keeps_allowed_hosts_and_parent_domains():
    state = {
        "cookies": [
            {"name": "a", "value": "1", "domain": "app.example.com", "path": "/"},
            {"name": "b", "value": "2", "domain": ".example.com", "path": "/"},
            {"name": "c", "value": "3", "domain": "evil.com", "path": "/"},
            {"name": "d", "value": "4", "domain": "other.example.com", "path": "/"},
            {"name": "e", "value": "5", "domain": "notexample.com", "path": "/"},
        ],
        "origins": [
            {"origin": "https://app.example.com", "localStorage": [{"name": "t", "value": "x"}]},
            {"origin": "https://evil.com:8443", "localStorage": [{"name": "t", "value": "y"}]},
        ],
    }
    out = logins.filter_state(state, {"app.example.com"})
    assert [c["name"] for c in out["cookies"]] == ["a", "b"]
    assert [o["origin"] for o in out["origins"]] == ["https://app.example.com"]


def test_filter_state_ip_exact_public_suffix_and_https_origins():
    state = {
        "cookies": [
            {"name": "ip", "value": "1", "domain": "10.0.0.5", "path": "/"},
            {"name": "ipparent", "value": "2", "domain": "0.0.5", "path": "/"},
            {"name": "suffix", "value": "3", "domain": ".co.uk", "path": "/"},
            {"name": "tld", "value": "4", "domain": ".uk", "path": "/"},
            {"name": "parent", "value": "5", "domain": ".shop.co.uk", "path": "/"},
        ],
        "origins": [
            {"origin": "http://app.shop.co.uk", "localStorage": []},
            {"origin": "https://app.shop.co.uk", "localStorage": []},
        ],
    }
    out = logins.filter_state(state, {"10.0.0.5", "app.shop.co.uk"})
    assert [c["name"] for c in out["cookies"]] == ["ip", "parent"]
    assert [o["origin"] for o in out["origins"]] == ["https://app.shop.co.uk"]


def test_test_spki_needs_kpi_testing(monkeypatch):
    pin = "A" * 43 + "="  # base64 of 32 bytes
    monkeypatch.setenv(fetch_mod.TEST_TRUSTED_SPKI_ENV, pin)
    monkeypatch.delenv(fetch_mod.TESTING_ENV, raising=False)
    assert not any("spki" in a for a in fetch_mod._launch_options("http://p")["args"])
    monkeypatch.setenv(fetch_mod.TESTING_ENV, "1")
    assert f"--ignore-certificate-errors-spki-list={pin}" in fetch_mod._launch_options(
        "http://p")["args"]
    monkeypatch.setenv(fetch_mod.TEST_TRUSTED_SPKI_ENV, "not-a-pin")
    assert not any("spki" in a for a in fetch_mod._launch_options("http://p")["args"])


def test_save_and_load_state_encrypted_and_filtered(db_session):
    row = CredentialRow(name="Portal", kind="login", username=USER,
                        secret_enc=creds.encrypt(PASSWORD),
                        login_url="https://portal.example.com/login")
    db_session.add(row)
    db_session.flush()
    cred = creds.load(db_session, row.id)
    state = {"cookies": [
        {"name": "sid", "value": "cookie-value-123", "domain": "portal.example.com", "path": "/"},
        {"name": "ad", "value": "tracker-456", "domain": "ads.example.net", "path": "/"},
    ], "origins": []}
    assert logins.load_state(db_session, cred) is None
    logins.save_state(db_session, cred, state)
    raw = db_session.execute(text("SELECT state_enc FROM login_sessions WHERE credential_id=:i"),
                             {"i": row.id}).scalar_one()
    assert b"cookie-value-123" not in bytes(raw) and b"tracker-456" not in bytes(raw)
    assert json.loads(creds.decrypt(raw))["cookies"][0]["name"] == "sid"
    loaded = logins.load_state(db_session, cred)
    assert [c["name"] for c in loaded["cookies"]] == ["sid"]


# --- real browser ---------------------------------------------------------------------------

@browser
def test_sign_in_no_code(site):
    cred = cred_for(site)
    state = logins.sign_in(cred, None)
    assert "localhost" in cookie_hosts(state)
    res, _, expired = logins.browser_fetch(site.url("/data"), cred, state)
    assert res.status == "ok" and not expired and PROTECTED.encode() in res.body


@browser
def test_sign_in_needs_code_raises_without_provider(site):
    site.code_mode = True
    with pytest.raises(logins.LoginNeedsCode):
        logins.sign_in(cred_for(site), None)


@browser
def test_sign_in_with_code_provider(site):
    site.code_mode = True
    calls = []

    def provider():
        calls.append(time.monotonic())
        return code_from(site)() if len(calls) >= 2 else None  # the code "arrives" later

    cred = cred_for(site)
    state = logins.sign_in(cred, provider, timeout_s=30)
    assert len(calls) >= 2 and calls[1] - calls[0] >= 1.5  # polled about every 2 s
    res, _, expired = logins.browser_fetch(site.url("/data"), cred, state)
    assert res.status == "ok" and not expired and PROTECTED.encode() in res.body


@browser
def test_wrong_code_fails(site):
    site.code_mode = True
    with pytest.raises(logins.LoginFailed) as e:
        logins.sign_in(cred_for(site), lambda: "000000x", timeout_s=30)
    assert str(e.value) == "code was not accepted"
    assert "000000x" not in str(e.value)


@browser
def test_code_timeout(site):
    site.code_mode = True
    start = time.monotonic()
    with pytest.raises(logins.LoginFailed) as e:
        logins.sign_in(cred_for(site), lambda: None, timeout_s=3)
    assert str(e.value) == "timed out waiting for the code"
    assert time.monotonic() - start < 30


@browser
def test_wrong_password_still_on_login_page(site):
    cred = cred_for(site)
    cred.secret = "Wrong-password-99"
    with pytest.raises(logins.LoginFailed) as e:
        logins.sign_in(cred, None)
    assert str(e.value) == "login failed: still on the login page"


@browser
def test_browser_fetch_with_valid_session(site):
    cred = cred_for(site)
    state = logins.sign_in(cred, None)
    res, new_state, expired = logins.browser_fetch(site.url("/data"), cred, state)
    assert res.status == "ok" and not expired
    assert PROTECTED.encode() in res.body
    assert new_state is not None and "localhost" in cookie_hosts(new_state)
    # without a session the same page is not returned as ok
    res, _, expired = logins.browser_fetch(site.url("/data"), cred, None)
    assert res.status == "login_needed" and expired


@browser
def test_browser_fetch_detects_expiry(site):
    cred = cred_for(site)
    state = logins.sign_in(cred, None)
    site.expire_all()
    # redirected to the login page
    res, _, expired = logins.browser_fetch(site.url("/data"), cred, state)
    assert expired and res.status == "login_needed" and res.error == "login needed"
    assert res.body == b""
    # a login form shown in place, without a redirect
    res, _, expired = logins.browser_fetch(site.url("/inline"), cred, state)
    assert expired and res.status == "login_needed" and res.body == b""
    # the login page itself is never ok content
    res, _, expired = logins.browser_fetch(site.url("/login"), cred, state)
    assert expired and res.status == "login_needed"


@browser
def test_session_filtered_to_allowed_hosts(site, monkeypatch):
    raw = []
    real_filter = logins.filter_state
    monkeypatch.setattr(logins, "filter_state", lambda st, hosts: raw.append(st) or
                        real_filter(st, hosts))
    cred = cred_for(site)
    state = logins.sign_in(cred, None)
    # the login page loaded a pixel from 127.0.0.1 that set a `tracker` cookie there: the
    # browser stored it, but the returned state drops it
    assert any(c["name"] == "tracker" and c["domain"] == "127.0.0.1" for c in raw[-1]["cookies"])
    assert cookie_hosts(state) == {"localhost"}
    assert all(c["name"] != "tracker" for c in state["cookies"])
    # a session cookie for 127.0.0.1 is not applied: that host is not allowed for this login
    sid = next(c for c in state["cookies"] if c["name"] == "sid")
    foreign = {"cookies": [dict(sid, domain="127.0.0.1")], "origins": []}
    site.cookies_seen.clear()
    res, back, expired = logins.browser_fetch(site.url("/data", host="127.0.0.1"), cred, foreign)
    assert res.status != "ok" and PROTECTED.encode() not in res.body
    assert all("sid=" not in c for h, c in site.cookies_seen if h == "127.0.0.1")
    assert back == foreign  # an unused session is handed back unchanged
    # ...but it is once 127.0.0.1 is listed under extra_hosts
    cred2 = cred_for(site, extra_hosts=["127.0.0.1"])
    res, _, expired = logins.browser_fetch(site.url("/data", host="127.0.0.1"), cred2, foreign)
    assert res.status == "ok" and not expired


@browser
def test_form_detection_without_selectors(site):
    # /login has a search box outside the form, a hidden honeypot text input before the
    # password and a type=button before the submit button
    state = logins.sign_in(cred_for(site), None)
    assert any(c["name"] == "sid" for c in state["cookies"])


@browser
def test_custom_selectors(site):
    # username comes after the password and there is no submit button: detection fails...
    with pytest.raises(logins.LoginFailed):
        logins.sign_in(cred_for(site, path="/login-custom"), None)
    # ...explicit selectors work
    cred = cred_for(site, path="/login-custom",
                    selectors={"username": "#who", "password": "#pw", "submit": "#go"})
    state = logins.sign_in(cred, None)
    assert any(c["name"] == "sid" for c in state["cookies"])


@browser
def test_custom_code_selector(site):
    site.code_mode = True
    cred = cred_for(site, selectors={"code": "form[action='/verify'] input[name=otp]"})
    state = logins.sign_in(cred, code_from(site), timeout_s=30)
    assert any(c["name"] == "sid" for c in state["cookies"])


@browser
def test_login_url_must_be_https_and_allowed(site, monkeypatch):
    cred = cred_for(site)
    cred.login_url = cred.login_url.replace("https://", "http://")
    with pytest.raises(logins.LoginFailed) as e:
        logins.sign_in(cred, None)
    assert str(e.value) == "login page must be https"
    monkeypatch.setattr(get_settings(), "watch_allowed_hosts", "")
    with pytest.raises(logins.LoginFailed) as e:
        logins.sign_in(cred_for(site), None)
    assert str(e.value) == "destination not allowed"


@browser
def test_password_not_in_any_output(site, caplog):
    caplog.set_level(logging.DEBUG)
    cred = cred_for(site)
    outputs = []
    # a failing login whose page echoes the typed password
    bad = cred_for(site)
    bad.secret = "Wrong-password-99"
    with pytest.raises(logins.LoginFailed) as e:
        logins.sign_in(bad, None)
    outputs.append(str(e.value))
    assert "Wrong-password-99" not in str(e.value)
    # wrong code echoed by the site
    site.code_mode = True
    with pytest.raises(logins.LoginFailed) as e:
        logins.sign_in(cred, lambda: "987654", timeout_s=30)
    outputs.append(str(e.value))
    site.code_mode = False
    state = logins.sign_in(cred, None)
    # a protected page that echoes the password
    res, new_state, _ = logins.browser_fetch(site.url("/echo"), cred, state)
    assert res.status == "ok" and PROTECTED.encode() in res.body
    outputs += [res.body.decode(), res.error or "", res.content_type]
    blob = "\n".join(outputs) + caplog.text
    assert PASSWORD not in blob
    assert "987654" not in blob
    assert "***" in res.body.decode()
    sid = next(c["value"] for c in new_state["cookies"] if c["name"] == "sid")
    assert sid not in caplog.text


def _password_posted(site):
    return [(h, p) for h, p, body in site.posts if "password=" in body]


@browser
def test_login_redirect_to_other_site_types_nothing(site):
    # 127.0.0.1 passes the destination policy but is not a host of this login
    with pytest.raises(logins.LoginFailed) as e:
        logins.sign_in(cred_for(site, path="/login-redirect"), None)
    assert str(e.value) == "login failed: login page moved to another site"
    assert _password_posted(site) == []


@browser
def test_form_posting_to_other_site_types_nothing(site):
    with pytest.raises(logins.LoginFailed) as e:
        logins.sign_in(cred_for(site, path="/login-offsite"), None)
    assert str(e.value) == "login failed: login page moved to another site"
    assert site.posts == []


@browser
def test_wrong_password_with_promo_code_box_fails_fast(site):
    cred = cred_for(site, path="/login-promo")
    cred.secret = "Wrong-password-99"
    start = time.monotonic()
    with pytest.raises(logins.LoginFailed) as e:  # not LoginNeedsCode
        logins.sign_in(cred, None)
    assert str(e.value) == "login failed: still on the login page"
    assert time.monotonic() - start < 30


@browser
def test_unrelated_pin_box_after_code_is_success(site):
    site.code_mode = True
    site.pin_after_code = True
    state = logins.sign_in(cred_for(site), code_from(site), timeout_s=30)
    assert any(c["name"] == "sid" for c in state["cookies"])


@browser
def test_code_provider_error(site):
    site.code_mode = True

    def broken():
        raise RuntimeError("db down " + PASSWORD)

    with pytest.raises(logins.LoginFailed) as e:
        logins.sign_in(cred_for(site), broken, timeout_s=30)
    assert str(e.value) == "could not read the code"
    assert e.value.__cause__ is None and e.value.__suppress_context__
