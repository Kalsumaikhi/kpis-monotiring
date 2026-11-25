"""Polite fetching of watched URLs: robots.txt, 1 req/s per host, size cap, timeout."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urlsplit

import httpx

USER_AGENT = "KPI-Monitor/1.0"
TIMEOUT = 30.0
MAX_BYTES = 5 * 1024 * 1024
MIN_INTERVAL = 1.0  # seconds between requests to the same host


@dataclass
class FetchResult:
    status: str  # 'ok' | 'blocked' | 'error'
    content_type: str = ""
    body: bytes = b""
    error: str | None = None


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
            r = client.get(f"{key}/robots.txt", headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
            if r.status_code == 200:
                parser = robotparser.RobotFileParser()
                parser.parse(r.text.splitlines())
        except httpx.HTTPError:
            parser = None  # unreachable robots.txt = allowed
        state.robots[key] = parser
    return state.robots[key]


def _fetch_http(client: httpx.Client, url: str) -> FetchResult:
    try:
        with client.stream("GET", url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT) as r:
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
    try:
        with sync_playwright() as p:
            exe = os.environ.get("KPI_CHROMIUM_PATH")
            browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
            try:
                page = browser.new_page(user_agent=USER_AGENT)
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
    host and requests to a host are spaced; without it each call is independent."""
    state = state or FetchState()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return FetchResult("error", error="invalid URL")
    host = parts.netloc
    robots = _robots(client, state, parts.scheme, host)
    if robots is not None and not robots.can_fetch(USER_AGENT, url):
        return FetchResult("blocked", error="disallowed by robots.txt")
    if use_browser:
        return _fetch_browser(url)
    state.throttle(host)
    return _fetch_http(client, url)
