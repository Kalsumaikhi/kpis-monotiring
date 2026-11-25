"""Browser-mode SSRF regression tests against real local servers and a real Chromium.

127.0.0.1 is allowlisted (WATCH_ALLOWED_HOSTS); 127.0.0.2 is loopback and NOT allowed, so
any request that reaches it is a policy bypass. Skipped when Playwright/Chromium is missing.
"""

import http.server
import os
import threading

import httpx
import pytest

from app.config import get_settings
from app.connectors.watch import fetch as fetch_mod
from app.connectors.watch.fetch import fetch_target

pytest.importorskip("playwright.sync_api")


def _chromium_available() -> bool:
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            exe = os.environ.get("KPI_CHROMIUM_PATH")
            b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
            b.close()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="Chromium not launchable")


class _Handler(http.server.BaseHTTPRequestHandler):
    hits: list[tuple[str, str]] = []  # noqa: RUF012 - reset per test by the fixture

    def do_GET(self):
        host = self.headers.get("Host", "")
        self.hits.append((host, self.path))
        port = self.server.server_address[1]
        internal = f"http://127.0.0.2:{port}"
        if self.path.startswith("/robots"):
            self.send_response(404)
            self.end_headers()
            return
        redirects = {
            "/start": f"{internal}/secret",
            "/imgredir": f"{internal}/pixel",
            "/fetchredir": f"{internal}/api",
            "/hop": "/final",
        }
        if host.startswith("127.0.0.1") and self.path in redirects:
            self.send_response(302)
            self.send_header("Location", redirects[self.path])
            self.end_headers()
            return
        if self.path == "/page":
            body = (
                "<html><body><main>PUBLIC PAGE</main>"
                '<img src="/imgredir">'
                f'<img src="{internal}/direct">'
                '<iframe src="/start"></iframe>'
                "<script>"
                "fetch('/fetchredir').catch(()=>{});"
                f"try {{ new WebSocket('ws://127.0.0.2:{port}/ws'); }} catch (e) {{}}"
                "</script></body></html>"
            )
        elif self.path == "/final":
            body = "<html><body><main>FINAL PAGE</main></body></html>"
        else:
            body = f"<html><body><main>SECRET host={host} path={self.path}</main></body></html>"
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(get_settings(), "watch_allowed_hosts", "127.0.0.1")
    monkeypatch.setattr(fetch_mod, "MIN_INTERVAL", 0.0)
    _Handler.hits = []
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _internal_hits():
    return [h for h in _Handler.hits if h[0].startswith("127.0.0.2")]


def test_browser_redirect_to_disallowed_host_is_not_fetched(server):
    with httpx.Client(trust_env=False) as c:
        r = fetch_target(c, f"http://127.0.0.1:{server}/start", use_browser=True)
    assert r.status == "error" and r.error == fetch_mod.NOT_ALLOWED
    assert b"SECRET" not in r.body
    assert _internal_hits() == []


def test_browser_subresources_cannot_reach_disallowed_hosts(server):
    with httpx.Client(trust_env=False) as c:
        r = fetch_target(c, f"http://127.0.0.1:{server}/page", use_browser=True)
    assert r.status == "ok" and b"PUBLIC PAGE" in r.body
    assert b"SECRET" not in r.body
    # redirected <img>, redirected fetch(), iframe redirect, direct <img>, WebSocket
    assert _internal_hits() == []
    paths = {p for _, p in _Handler.hits}
    assert {"/imgredir", "/start"} <= paths  # the allowed first hops did happen


def test_browser_allowed_redirect_is_followed(server):
    with httpx.Client(trust_env=False) as c:
        r = fetch_target(c, f"http://127.0.0.1:{server}/hop", use_browser=True)
    assert r.status == "ok" and b"FINAL PAGE" in r.body
