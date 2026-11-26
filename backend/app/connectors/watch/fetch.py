"""Polite fetching of watched URLs: robots.txt, 1 req/s per host, size cap, timeout."""

from __future__ import annotations

import base64
import ipaddress
import logging
import os
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

import httpx

from app import credentials as creds
from app.config import get_settings

USER_AGENT = "KPI-Monitor/1.0"
TIMEOUT = 30.0
MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
MIN_INTERVAL = 1.0  # seconds between requests to the same host


@dataclass
class FetchResult:
    status: str  # 'ok' | 'blocked' | 'error' | 'key_rejected' | 'login_needed'
    content_type: str = ""
    body: bytes = b""
    error: str | None = None


log = logging.getLogger(__name__)
# httpx logs every request URL at INFO, which would include a query-string key.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)
NOT_ALLOWED = "destination not allowed"


def _allowlist() -> set[str]:
    return {h.strip().lower() for h in get_settings().watch_allowed_hosts.split(",") if h.strip()}


def _addresses(host: str) -> list[str] | None:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return None
    return [info[4][0].split("%")[0] for info in infos] or None


def _address_allowed(addr: str, allowed: set[str]) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if addr.lower() in allowed:
        return True
    return ip.is_global and not ip.is_multicast


def destination_allowed(host: str) -> bool:
    """False if the host resolves to any non-public address (SSRF guard).

    WATCH_ALLOWED_HOSTS entries bypass the check. Note: DNS is resolved again by the HTTP
    client, so a rebinding race is not fully closed (browser mode pins the checked address).
    """
    allowed = _allowlist()
    if host.lower() in allowed:
        return True
    addrs = _addresses(host)
    return bool(addrs) and all(_address_allowed(a, allowed) for a in addrs)


def checked_addresses(host: str) -> list[str] | None:
    """The addresses to connect to for `host` if the destination policy allows it, else None.
    Resolved once, so the caller connects to exactly what was checked."""
    allowed = _allowlist()
    addrs = _addresses(host)
    if not addrs:
        return None
    if host.lower() in allowed or all(_address_allowed(a, allowed) for a in addrs):
        return addrs
    return None


class FetchState:
    """Per-download state: robots.txt cache and last request time per host."""

    def __init__(self) -> None:
        self.robots: dict[str, robotparser.RobotFileParser | None] = {}
        self.last_request: dict[str, float] = {}

    def throttle(self, host: str) -> None:
        last = self.last_request.get(host)
        if last is not None:
            wait = MIN_INTERVAL - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self.last_request[host] = time.monotonic()


def _robots(client: httpx.Client, state: FetchState, scheme: str, host: str):
    key = f"{scheme}://{host}"
    if key not in state.robots:
        parser = None
        try:
            state.throttle(host)
            r = client.get(f"{key}/robots.txt", headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
                           follow_redirects=False)
            if r.status_code == 200:
                parser = robotparser.RobotFileParser()
                parser.parse(r.text.splitlines())
        except httpx.HTTPError:
            parser = None  # unreachable robots.txt = allowed
        state.robots[key] = parser
    return state.robots[key]


def _fetch_http(client: httpx.Client, url: str, req_url: str | None = None,
                headers: dict | None = None, keyed: bool = False) -> FetchResult:
    """One request without following redirects. A redirect comes back as status 'redirect'
    with the (absolute) target in `error`. `req_url`/`headers` carry a key (url is the
    key-free form used to resolve redirects); `keyed` turns a 401/403 into key_rejected."""
    deadline = time.monotonic() + TIMEOUT
    try:
        with client.stream("GET", req_url or url, headers=headers or {"User-Agent": USER_AGENT},
                           timeout=TIMEOUT, follow_redirects=False) as r:
            if r.is_redirect and r.headers.get("location"):
                return FetchResult("redirect", error=urljoin(url, r.headers["location"]))
            if keyed and r.status_code in (401, 403):
                return FetchResult("key_rejected", error=f"key rejected (HTTP {r.status_code})")
            if r.status_code >= 400:
                return FetchResult("error", error=f"HTTP {r.status_code}")
            ctype = r.headers.get("content-type", "")
            declared = r.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_BYTES:
                return FetchResult("error", ctype, error="too large")
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_BYTES:
                    return FetchResult("error", ctype, error="too large")
                if time.monotonic() > deadline:
                    return FetchResult("error", ctype, error="timeout")
            return FetchResult("ok", ctype, bytes(buf))
    except httpx.TimeoutException:
        return FetchResult("error", error="timeout")
    except httpx.HTTPError as e:
        return FetchResult("error", error=f"{type(e).__name__}")


BROWSER_ARGS = [
    "--disable-quic",
    # WebRTC UDP does not go through the proxy; keep it from reaching internal hosts.
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
]


# TEST ONLY: base64 SHA-256 of one certificate's public key that Chromium should accept
# although no CA signed it (the login-engine tests' self-signed site). Honoured only when
# KPI_TESTING=1 is also set. Never set either in production; it trusts only that key, not
# "any certificate".
TEST_TRUSTED_SPKI_ENV = "KPI_TEST_TRUSTED_SPKI"
TESTING_ENV = "KPI_TESTING"
_spki_warned = False


def _test_spki() -> str | None:
    global _spki_warned
    spki = os.environ.get(TEST_TRUSTED_SPKI_ENV, "").strip()
    if not spki or os.environ.get(TESTING_ENV) != "1":
        return None
    try:
        if len(base64.b64decode(spki, validate=True)) != 32:
            return None
    except ValueError:
        return None
    if not _spki_warned:
        _spki_warned = True
        log.warning("%s is set: the browser trusts one extra certificate key (tests only)",
                    TEST_TRUSTED_SPKI_ENV)
    return spki


def _launch_options(proxy_url: str) -> dict:
    args = list(BROWSER_ARGS)
    spki = _test_spki()
    if spki:
        args.append(f"--ignore-certificate-errors-spki-list={spki}")
    launch: dict = {"proxy": {"server": proxy_url}, "args": args}
    exe = os.environ.get("KPI_CHROMIUM_PATH")
    if exe:
        launch["executable_path"] = exe
    return launch


@contextmanager
def browser_context(storage_state: dict | None = None) -> Iterator:
    """A fresh headless Chromium context whose every request goes through a new PolicyProxy
    (destination policy per request, incl. CONNECT; WebRTC UDP disabled). `storage_state`
    seeds cookies/localStorage. Raises ImportError when Playwright is not installed. The
    browser and the proxy are closed on exit."""
    from playwright.sync_api import sync_playwright

    from app.connectors.watch.proxy import PolicyProxy

    proxy = PolicyProxy()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**_launch_options(proxy.url))
            try:
                opts: dict = {"user_agent": USER_AGENT}
                if storage_state is not None:
                    opts["storage_state"] = storage_state
                yield browser.new_context(**opts)
            finally:
                browser.close()
    finally:
        proxy.close()


def _fetch_browser(url: str, nav_blocked=None) -> FetchResult:
    """Render `url` in headless Chromium.

    SSRF policy has two layers. `page.route` sees only the first request of a redirect
    chain (Chromium follows 3xx itself without re-entering the route), so every byte the
    browser sends goes through `PolicyProxy`, which checks destination_allowed per request
    (plain HTTP, CONNECT for HTTPS and WebSockets) and connects to the checked address.
    Main-frame navigations are fetched with redirects off: a 3xx comes back as status
    'redirect' so fetch_target re-checks policy and robots.txt and renders the next hop.
    `nav_blocked(url)` vets client-side navigations (meta refresh / script) the same way."""
    try:
        from playwright.sync_api import Error as PlaywrightError
    except ImportError:
        return FetchResult("error", error="browser support not installed")

    redirect: list[str] = []

    def guard(route):
        req = route.request
        parts = urlsplit(req.url)
        if parts.scheme in ("data", "blob", "about"):
            route.continue_()
            return
        if not (parts.scheme in ("http", "https") and parts.hostname
                and destination_allowed(parts.hostname)):
            route.abort()
            return
        if req.is_navigation_request() and req.frame.parent_frame is None:
            if req.url != url and nav_blocked is not None and nav_blocked(req.url):
                route.abort()
                return
            resp = route.fetch(max_redirects=0)
            location = resp.headers.get("location")
            if 300 <= resp.status < 400 and location:
                redirect.append(urljoin(req.url, location))
                route.abort()
                return
            route.fulfill(response=resp)
            return
        route.continue_()

    try:
        with browser_context() as context:
            page = context.new_page()
            page.route("**/*", guard)
            try:
                page.goto(url, timeout=int(TIMEOUT * 1000), wait_until="networkidle")
            except PlaywrightError:
                if redirect:
                    return FetchResult("redirect", error=redirect[0])
                raise
            html = page.content()
    except Exception as e:  # noqa: BLE001 - playwright raises its own error types
        return FetchResult("error", error=f"browser error: {type(e).__name__}")
    body = html.encode("utf-8")
    if len(body) > MAX_BYTES:
        return FetchResult("error", "text/html", error="too large")
    return FetchResult("ok", "text/html; charset=utf-8", body)


def _nav_blocked(client: httpx.Client, state: FetchState, url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return True
    if not destination_allowed(parts.hostname):
        return True
    robots = _robots(client, state, parts.scheme, parts.netloc)
    return robots is not None and not robots.can_fetch(USER_AGENT, url)


NEEDS_HTTPS = "keys can only be sent over https"
NO_BROWSER_KEY = 'keys can\'t be used with "Needs a browser" yet'
LOGIN_ENGINE_ONLY = "login keys are only used by the browser login"


def preflight(client: httpx.Client, state: FetchState, url: str) -> FetchResult | None:
    """The checks fetch_target makes before a request, for callers that render the page
    themselves (logins.browser_fetch): scheme, destination policy (fail closed), robots.txt
    and the per-host throttle. Returns the refusal, or None when the fetch may go ahead
    (the throttle slot is then taken)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return FetchResult("error", error="invalid URL")
    if not destination_allowed(parts.hostname):
        return FetchResult("error", error=NOT_ALLOWED)
    robots = _robots(client, state, parts.scheme, parts.netloc)
    if robots is not None and not robots.can_fetch(USER_AGENT, url):
        return FetchResult("blocked", error="disallowed by robots.txt")
    state.throttle(parts.netloc)
    return None


def _origin(parts) -> tuple[str, int | None]:
    return (parts.hostname or "").lower(), parts.port


def fetch_target(client: httpx.Client, url: str, use_browser: bool = False,
                 state: FetchState | None = None,
                 credential: creds.Credential | None = None) -> FetchResult:
    """Fetch one URL. `state` is shared across one download so robots.txt is read once per
    host and requests to a host are spaced; without it each call is independent.

    Redirects are followed manually (max 5); scheme, destination policy, robots.txt and
    throttling are re-checked on every hop (in browser mode too, for the main-frame
    navigation; everything else the browser loads is policed by the proxy).

    A `credential` is sent only over https and only to the original host (same port) on
    every hop; a hop elsewhere is fetched without it (a query key echoed in a Location is
    stripped before every hop). Error text and the body are scrubbed
    of the secret before they leave this function."""
    res = _fetch_target(client, url, use_browser, state or FetchState(), credential)
    if credential is not None:
        if res.error:
            res.error = creds.redact(res.error, credential)
        if res.body:
            res.body = creds.redact_bytes(res.body, credential)
    return res


def _fetch_target(client, url, use_browser, state, credential) -> FetchResult:
    origin = _origin(urlsplit(url))
    if credential is not None:
        if credential.kind == "login":  # sessions go through logins.browser_fetch only
            return FetchResult("error", error=LOGIN_ENGINE_ONLY)
        if use_browser:
            return FetchResult("error", error=NO_BROWSER_KEY)
        if urlsplit(url).scheme != "https":
            return FetchResult("error", error=NEEDS_HTTPS)
    for hop in range(MAX_REDIRECTS + 1):
        # A Location may echo `param_name=<secret>`; never follow that as-is. apply() puts
        # the key back on keyed hops only.
        url = creds.strip_query_secret(credential, url)
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return FetchResult("error", error="invalid URL")
        if not destination_allowed(parts.hostname):
            return FetchResult("error", error=NOT_ALLOWED)
        robots = _robots(client, state, parts.scheme, parts.netloc)
        if robots is not None and not robots.can_fetch(USER_AGENT, url):
            return FetchResult("blocked", error="disallowed by robots.txt")
        state.throttle(parts.netloc)
        if use_browser:
            res = _fetch_browser(url, lambda u: _nav_blocked(client, state, u))
        else:
            keyed = (credential is not None and parts.scheme == "https"
                     and _origin(parts) == origin)
            if keyed:
                req_url, headers = creds.apply(credential, url, {"User-Agent": USER_AGENT})
                res = _fetch_http(client, url, req_url, headers, keyed=True)
            else:
                res = _fetch_http(client, url)
        if res.status != "redirect":
            return res
        url = res.error
    return FetchResult("error", error="too many redirects")
