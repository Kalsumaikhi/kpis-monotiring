"""Browser login engine: sign in with a stored username/password, an optional emailed one-time
code, and fetch protected pages with the saved session (Playwright storage_state).

Every browser here comes from `fetch.browser_context`, so each request goes through the
destination-policy proxy (CONNECT included). Sessions are filtered to the login's allowed hosts
(`credentials.allowed_hosts`) before they are saved, loaded, returned or applied, and are only
applied to https URLs on those hosts. Nothing returned or raised carries the password or the
code: errors are fixed strings (plus an exception type name) and page bodies are redacted.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app import credentials as creds
from app.connectors.watch.fetch import (
    MAX_BYTES,
    NOT_ALLOWED,
    TIMEOUT,
    FetchResult,
    browser_context,
    destination_allowed,
)
from app.models import LoginSession

log = logging.getLogger(__name__)

SUBMIT_WAIT_S = 15.0  # after a submit: wait this long for a navigation or network idle
CODE_POLL_S = 2.0
LOGIN_NEEDED = "login needed"
MARK = "data-kpi-login"

_VISIBLE_JS = """el => { if (!el) return false; const s = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0; }"""

# Marks the first visible password box; returns whether there is one.
_FIND_PASSWORD_JS = f"""() => {{ const vis = {_VISIBLE_JS};
  document.querySelectorAll('[{MARK}]').forEach(e => e.removeAttribute('{MARK}'));
  const pw = [...document.querySelectorAll('input[type=password]')].find(vis);
  if (!pw) return false;
  pw.setAttribute('{MARK}', 'password');
  return true; }}"""

# Given the password box: mark the nearest preceding visible email/text/tel input in the same
# form (the document when there is no form) and the form's first submit button.
_MARK_REST_JS = f"""pw => {{ const vis = {_VISIBLE_JS};
  const scope = pw.form ? [...pw.form.elements] : [...document.querySelectorAll('input')];
  let user = null;
  for (const el of scope) {{
    if (el === pw) break;
    if (el.tagName !== 'INPUT') continue;
    const t = (el.getAttribute('type') || 'text').toLowerCase();
    if (['email', 'text', 'tel'].includes(t) && vis(el)) user = el;
  }}
  if (user) user.setAttribute('{MARK}', 'username');
  const submit = pw.form
    ? pw.form.querySelector('button[type=submit], input[type=submit]') : null;
  if (submit) submit.setAttribute('{MARK}', 'submit');
  return {{user: !!user, submit: !!submit}}; }}"""

# Marks the first visible one-time-code box; returns whether there is one.
_FIND_CODE_JS = f"""() => {{ const vis = {_VISIBLE_JS};
  document.querySelectorAll('[{MARK}=code]').forEach(e => e.removeAttribute('{MARK}'));
  const skip = ['password', 'hidden', 'submit', 'button', 'checkbox', 'radio', 'image',
                'reset', 'file'];
  const rx = /otp|code|token|verif|pin/i;
  for (const el of document.querySelectorAll('input')) {{
    const t = (el.getAttribute('type') || 'text').toLowerCase();
    if (skip.includes(t) || !vis(el)) continue;
    const ac = (el.getAttribute('autocomplete') || '').toLowerCase().split(/\\s+/);
    const named = ['name', 'id', 'placeholder'].some(a => rx.test(el.getAttribute(a) || ''));
    const ml = parseInt(el.getAttribute('maxlength') || '', 10);
    const numeric = (el.getAttribute('inputmode') || '').toLowerCase() === 'numeric'
      && ml > 0 && ml <= 8;
    if (ac.includes('one-time-code') || named || numeric) {{
      el.setAttribute('{MARK}', 'code');
      return true;
    }}
  }}
  return false; }}"""

_HAS_VISIBLE_PASSWORD_JS = f"""() => {{ const vis = {_VISIBLE_JS};
  return [...document.querySelectorAll('input[type=password]')].some(vis); }}"""

# The code box's form's submit button, marked; false when there is none.
_MARK_CODE_SUBMIT_JS = f"""el => {{ const b = el.form
    ? el.form.querySelector('button[type=submit], input[type=submit]') : null;
  if (b) b.setAttribute('{MARK}', 'code-submit');
  return !!b; }}"""


class LoginNeedsCode(Exception):
    """The site asked for an emailed code and no code provider was given."""


class LoginFailed(Exception):
    """Sign-in failed. The message is a fixed string (already redacted)."""


def _redact(text: str, cred: creds.Credential, code: str | None = None) -> str:
    text = creds.redact(text, cred)
    if code:
        text = creds.redact(text, replace(cred, kind="bearer", secret=code))
    return text


def _fail(msg: str, cred: creds.Credential, code: str | None = None) -> LoginFailed:
    return LoginFailed(_redact(msg, cred, code))


# --- session state --------------------------------------------------------------------------

def _host_matches(cookie_domain: str, hosts: set[str]) -> bool:
    d = cookie_domain.lower().lstrip(".")
    return bool(d) and any(h == d or h.endswith("." + d) for h in hosts)


def filter_state(state: dict, hosts: set[str]) -> dict:
    """Keep cookies whose domain is one of `hosts` or a parent domain of one, and origins
    (localStorage) whose host is in `hosts`."""
    hosts = {h.lower() for h in hosts}
    cookies = [c for c in state.get("cookies") or [] if _host_matches(c.get("domain", ""), hosts)]
    origins = [o for o in state.get("origins") or []
               if (urlsplit(o.get("origin", "")).hostname or "").lower() in hosts]
    return {"cookies": cookies, "origins": origins}


def load_state(session: Session, cred: creds.Credential) -> dict | None:
    """The saved session, filtered to the login's allowed hosts; None when there is none or it
    cannot be read (then a fresh sign-in is needed)."""
    row = session.get(LoginSession, cred.id)
    if row is None or not row.state_enc:
        return None
    try:
        state = json.loads(creds.decrypt(row.state_enc))
    except (creds.KeyUnavailable, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    return filter_state(state, creds.allowed_hosts(cred))


def save_state(session: Session, cred: creds.Credential, state: dict) -> None:
    """Store the session (filtered to allowed hosts) encrypted. Flushes; the caller commits."""
    data = json.dumps(filter_state(state, creds.allowed_hosts(cred)))
    row = session.get(LoginSession, cred.id)
    if row is None:
        row = LoginSession(credential_id=cred.id)
        session.add(row)
    row.state_enc = creds.encrypt(data)
    session.flush()


# --- browser helpers ------------------------------------------------------------------------

def _guard(route) -> None:
    """Defence in depth on top of the proxy: only http(s) to allowed destinations."""
    parts = urlsplit(route.request.url)
    if parts.scheme in ("data", "blob", "about") or parts.scheme in ("http", "https") and parts.hostname and destination_allowed(
            parts.hostname):
        route.continue_()
    else:
        route.abort()


def _settle(page, timeout_s: float) -> None:
    from playwright.sync_api import TimeoutError as PWTimeout

    if timeout_s <= 0:
        return
    try:
        page.wait_for_load_state("networkidle", timeout=int(timeout_s * 1000))
    except PWTimeout:
        pass


def _submit_and_wait(page, action: Callable[[], None]) -> None:
    """Run `action` (click or Enter); wait up to SUBMIT_WAIT_S for a navigation, then idle."""
    from playwright.sync_api import TimeoutError as PWTimeout

    start = time.monotonic()
    try:
        with page.expect_navigation(timeout=int(SUBMIT_WAIT_S * 1000), wait_until="load"):
            action()
    except PWTimeout:
        pass  # no navigation (single-page app): fall through to network idle
    _settle(page, min(5.0, SUBMIT_WAIT_S - (time.monotonic() - start)))


def _host_path(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return (parts.hostname or "").lower(), parts.path or "/"


def _on_login_page(page, cred: creds.Credential) -> bool:
    """Global Constraints expiry rule: the login page's host+path, or a visible password box."""
    if cred.login_url and _host_path(page.url) == _host_path(cred.login_url):
        return True
    return bool(page.evaluate(_HAS_VISIBLE_PASSWORD_JS))


def _wait_visible(page, selector: str, timeout_s: float = 10.0):
    from playwright.sync_api import TimeoutError as PWTimeout

    loc = page.locator(selector).first
    try:
        loc.wait_for(state="visible", timeout=int(timeout_s * 1000))
    except PWTimeout:
        return None
    return loc


def _code_box(page, sel: dict):
    if sel.get("code"):
        loc = page.locator(sel["code"]).first
        return loc if loc.is_visible() else None
    if page.evaluate(_FIND_CODE_JS):
        return page.locator(f"[{MARK}=code]").first
    return None


# --- public API -----------------------------------------------------------------------------

def browser_fetch(url: str, cred: creds.Credential,
                  state: dict | None) -> tuple[FetchResult, dict | None, bool]:
    """Render `url` with the login's session. Returns (result, new state, expired).

    The session is applied only when `url` is https on a host allowed for the login;
    otherwise the page is fetched without it and `state` is handed back unchanged. A page
    that is the login page or shows a visible password box is never returned as ok: it comes
    back as status 'login_needed' (error 'login needed'), empty body, expired=True. Robots.txt
    and per-host throttling are the caller's job (as for fetch_target)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        return FetchResult("error", error="invalid URL"), state, False
    if not destination_allowed(host):
        return FetchResult("error", error=NOT_ALLOWED), state, False
    hosts = creds.allowed_hosts(cred)
    use_session = parts.scheme == "https" and host in hosts
    seed = filter_state(state, hosts) if (use_session and state) else None
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return FetchResult("error", error="browser support not installed"), state, False

    def new_state(context):
        return filter_state(context.storage_state(), hosts) if use_session else state

    deadline = time.monotonic() + TIMEOUT
    try:
        with browser_context(seed) as context:
            page = context.new_page()
            page.route("**/*", _guard)
            resp = page.goto(url, timeout=int(TIMEOUT * 1000), wait_until="load")
            _settle(page, min(10.0, deadline - time.monotonic()))
            if _on_login_page(page, cred):
                return FetchResult("login_needed", error=LOGIN_NEEDED), new_state(context), True
            if resp is not None and resp.status >= 400:
                return FetchResult("error", error=f"HTTP {resp.status}"), state, False
            html = page.content()
            out_state = new_state(context)
    except Exception as e:  # noqa: BLE001 - playwright raises its own error types
        return FetchResult("error", error=f"browser error: {type(e).__name__}"), state, False
    body = html.encode("utf-8")
    if len(body) > MAX_BYTES:
        return FetchResult("error", "text/html", error="too large"), state, False
    return (FetchResult("ok", "text/html; charset=utf-8", creds.redact_bytes(body, cred)),
            out_state, False)


def sign_in(cred: creds.Credential, code_provider: Callable[[], str | None] | None,
            timeout_s: float = 600) -> dict:
    """Sign in at `cred.login_url` and return the new storage_state (filtered to the login's
    allowed hosts).

    Raises LoginNeedsCode when a code box appears and `code_provider` is None. With a
    provider, polls it every 2 s until it returns a code or `timeout_s` passes, then types
    the code and submits. Raises LoginFailed (fixed, redacted messages) otherwise."""
    url = cred.login_url or ""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise _fail("login page must be https", cred)
    if not destination_allowed(parts.hostname):
        raise _fail(NOT_ALLOWED, cred)
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        raise _fail("login failed: browser support not installed", cred) from None
    sel = cred.selectors or {}
    code: str | None = None
    try:
        with browser_context() as context:
            page = context.new_page()
            page.route("**/*", _guard)
            page.goto(url, timeout=int(TIMEOUT * 1000), wait_until="load")
            _settle(page, 10.0)

            # The form: explicit selectors win, the rest is detected around the password box.
            if sel.get("password"):
                pw = _wait_visible(page, sel["password"])
                if pw is None:
                    raise _fail("login failed: password box not found", cred)
            else:
                if not page.evaluate(_FIND_PASSWORD_JS):
                    raise _fail("login failed: no login form found", cred)
                pw = page.locator(f"[{MARK}=password]").first
            found = pw.evaluate(_MARK_REST_JS)
            if sel.get("username"):
                user = _wait_visible(page, sel["username"])
            else:
                user = page.locator(f"[{MARK}=username]").first if found["user"] else None
            if user is None:
                raise _fail("login failed: username box not found", cred)
            if sel.get("submit"):
                submit = _wait_visible(page, sel["submit"])
                if submit is None:
                    raise _fail("login failed: submit button not found", cred)
            else:
                submit = page.locator(f"[{MARK}=submit]").first if found["submit"] else None

            user.fill(cred.username or "")
            pw.fill(cred.secret)
            _submit_and_wait(page, submit.click if submit is not None
                             else (lambda: pw.press("Enter")))

            box = _code_box(page, sel)
            if box is not None:
                if code_provider is None:
                    raise LoginNeedsCode("the site asks for the emailed code")
                deadline = time.monotonic() + timeout_s
                while not (code := code_provider()):
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise _fail("timed out waiting for the code", cred)
                    time.sleep(min(CODE_POLL_S, left))
                box.fill(code)
                if box.evaluate(_MARK_CODE_SUBMIT_JS):
                    action = page.locator(f"[{MARK}=code-submit]").first.click
                else:
                    action = lambda: box.press("Enter")
                _submit_and_wait(page, action)
                if _code_box(page, sel) is not None:
                    raise _fail("code was not accepted", cred, code)

            if _on_login_page(page, cred):
                raise _fail("login failed: still on the login page", cred, code)
            return filter_state(context.storage_state(), creds.allowed_hosts(cred))
    except (LoginNeedsCode, LoginFailed):
        raise
    except Exception as e:  # noqa: BLE001 - playwright raises its own error types
        raise _fail(f"login failed: browser error: {type(e).__name__}", cred, code) from None
