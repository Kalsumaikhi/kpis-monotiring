"""Polite fetching of watched URLs: robots.txt, 1 req/s per host, size cap, timeout."""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import time
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import get_settings

USER_AGENT = "KPI-Monitor/1.0"
TIMEOUT = 30.0
MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
MIN_INTERVAL = 1.0  # seconds between requests to the same host


@dataclass
class FetchResult:
    status: str  # 'ok' | 'blocked' | 'error'
    content_type: str = ""
    body: bytes = b""
    error: str | None = None


log = logging.getLogger(__name__)
NOT_ALLOWED = "destination not allowed"


def destination_allowed(host: str) -> bool:
    """False if the host resolves to any non-public address (SSRF guard).

    WATCH_ALLOWED_HOSTS entries bypass the check. Note: DNS is resolved again by the HTTP
    client, so a rebinding race is not fully closed.
    """
    allowed = {h.strip().lower() for h in get_settings().watch_allowed_hosts.split(",") if h.strip()}
    if host.lower() in allowed:
        return True
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        addr = info[4][0].split("%")[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if addr.lower() in allowed:
            continue
        if not ip.is_global or ip.is_multicast:
            return False
    return True


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


def _fetch_http(client: httpx.Client, url: str) -> FetchResult:
    """One request without following redirects. A redirect comes back as status 'redirect'
    with the (absolute) target in `error`."""
    deadline = time.monotonic() + TIMEOUT
    try:
        with client.stream("GET", url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
                           follow_redirects=False) as r:
            if r.is_redirect and r.headers.get("location"):
                return FetchResult("redirect", error=urljoin(url, r.headers["location"]))
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


def _fetch_browser(url: str) -> FetchResult:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return FetchResult("error", error="browser support not installed")

    def guard(route):
        parts = urlsplit(route.request.url)
        if parts.scheme in ("data", "blob", "about"):
            route.continue_()
        elif parts.scheme in ("http", "https") and parts.hostname and destination_allowed(parts.hostname):
            route.continue_()
        else:
            route.abort()

    try:
        with sync_playwright() as p:
            exe = os.environ.get("KPI_CHROMIUM_PATH")
            browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
            try:
                page = browser.new_page(user_agent=USER_AGENT)
                page.route("**/*", guard)
                page.goto(url, timeout=int(TIMEOUT * 1000), wait_until="networkidle")
                html = page.content()
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001 - playwright raises its own error types
        return FetchResult("error", error=f"browser error: {type(e).__name__}")
    body = html.encode("utf-8")
    if len(body) > MAX_BYTES:
        return FetchResult("error", "text/html", error="too large")
    return FetchResult("ok", "text/html; charset=utf-8", body)


def fetch_target(client: httpx.Client, url: str, use_browser: bool = False,
                 state: FetchState | None = None) -> FetchResult:
    """Fetch one URL. `state` is shared across one download so robots.txt is read once per
    host and requests to a host are spaced; without it each call is independent.

    Redirects are followed manually (max 5); scheme, destination policy, robots.txt and
    throttling are re-checked on every hop."""
    state = state or FetchState()
    for hop in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return FetchResult("error", error="invalid URL")
        if not destination_allowed(parts.hostname):
            return FetchResult("error", error=NOT_ALLOWED)
        robots = _robots(client, state, parts.scheme, parts.netloc)
        if robots is not None and not robots.can_fetch(USER_AGENT, url):
            return FetchResult("blocked", error="disallowed by robots.txt")
        if use_browser:
            return _fetch_browser(url)
        state.throttle(parts.netloc)
        res = _fetch_http(client, url)
        if res.status != "redirect":
            return res
        url = res.error
    return FetchResult("error", error="too many redirects")
