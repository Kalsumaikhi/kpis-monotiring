import json
from pathlib import Path

import httpx
import pytest
import respx

from app.connectors.watch import fetch as fetch_mod
from app.connectors.watch.excerpt import make_excerpt
from app.connectors.watch.extract import extract_page, flatten_json, page_text
from app.connectors.watch.fetch import fetch_target

FX = Path(__file__).parent / "fixtures" / "watch"


def _fake_gai(host, *a, **k):
    import ipaddress
    try:
        ipaddress.ip_address(host)
        ip = host
    except ValueError:
        ip = {"localhost": "127.0.0.1", "meta.example": "169.254.169.254"}.get(host, "93.184.216.34")
    return [(2, 1, 6, "", (ip, 0))]


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    monkeypatch.setattr(fetch_mod.socket, "getaddrinfo", _fake_gai)


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    monkeypatch.setattr(fetch_mod, "MIN_INTERVAL", 0.0)


def test_page_text_main_and_selector():
    html = (FX / "page.html").read_text()
    text = page_text(html, None)
    assert "Revenue grew by 5%." in text
    assert "Second paragraph." in text
    assert "Quarterly report" in text
    assert "Nav link" not in text and "Footer" not in text and "Sidebar" not in text
    assert "var a" not in text
    assert "" not in text.split("\n")  # no blank lines
    sel, err = extract_page(html, "#kpi")
    assert sel == "Active users: 1,234" and err is None
    miss, err = extract_page(html, "#nope")
    assert miss == "" and err and "selector" in err


def test_flatten_json_paths():
    assert flatten_json(json.loads((FX / "data.json").read_text())) == {"a.b[0].c": "1", "d": "null"}


def test_excerpt_limits():
    ex = make_excerpt("keep", "keep\n" + "\n".join(f"new {i}" for i in range(12)))
    assert len(ex["added"]) == 5 and ex["removed"] == []
    ex = make_excerpt("x", "y" * 300)
    assert len(ex["added"][0]) == 200 and ex["removed"] == ["x"]


@respx.mock
def test_robots_disallow_blocks():
    respx.get("https://h.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n"))
    private = respx.get("https://h.example/private/x").mock(return_value=httpx.Response(200, text="x"))
    with httpx.Client() as c:
        r = fetch_target(c, "https://h.example/private/x")
    assert r.status == "blocked"
    assert not private.called


@respx.mock
def test_robots_missing_allows_and_user_agent():
    respx.get("https://h.example/robots.txt").mock(return_value=httpx.Response(404))
    page = respx.get("https://h.example/p").mock(
        return_value=httpx.Response(200, text="<p>hi</p>", headers={"content-type": "text/html"}))
    with httpx.Client() as c:
        r = fetch_target(c, "https://h.example/p")
    assert r.status == "ok" and r.body == b"<p>hi</p>"
    assert page.calls[0].request.headers["user-agent"] == "KPI-Monitor/1.0"


@respx.mock
def test_size_cap():
    respx.get("https://h.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://h.example/big").mock(
        return_value=httpx.Response(200, content=b"a" * (6 * 1024 * 1024)))
    with httpx.Client() as c:
        r = fetch_target(c, "https://h.example/big")
    assert r.status == "error" and r.error == "too large"


@respx.mock
def test_http_error_and_network_error():
    respx.get("https://h.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://h.example/e").mock(return_value=httpx.Response(500))
    respx.get("https://h.example/n").mock(side_effect=httpx.ConnectError("nope"))
    with httpx.Client() as c:
        assert fetch_target(c, "https://h.example/e").error == "HTTP 500"
        assert fetch_target(c, "https://h.example/n").status == "error"


def test_browser_support_missing(monkeypatch):
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("playwright"):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    with httpx.Client() as c:
        r = fetch_target(c, "https://h.example/p", use_browser=True)
    assert r.status == "error" and r.error == "browser support not installed"


@respx.mock
@pytest.mark.parametrize("url", ["http://localhost/x", "http://169.254.169.254/latest/meta-data",
                                 "http://meta.example/x", "http://10.0.0.5/x", "http://[::1]/x"])
def test_internal_destinations_refused(url):
    route = respx.route().mock(return_value=httpx.Response(200, text="secret"))
    with httpx.Client() as c:
        r = fetch_target(c, url)
    assert r.status == "error" and r.error == "destination not allowed"
    assert not route.called


@respx.mock
def test_redirect_to_internal_refused():
    respx.get("https://h.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://h.example/go").mock(
        return_value=httpx.Response(302, headers={"location": "http://127.0.0.1/admin"}))
    inner = respx.get("http://127.0.0.1/admin").mock(return_value=httpx.Response(200, text="x"))
    with httpx.Client(follow_redirects=True) as c:
        r = fetch_target(c, "https://h.example/go")
    assert r.error == "destination not allowed" and not inner.called


@respx.mock
def test_redirect_followed_and_robots_rechecked():
    respx.get("https://h.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n"))
    respx.get("https://h.example/go").mock(
        return_value=httpx.Response(302, headers={"location": "/private/x"}))
    respx.get("https://h.example/ok").mock(
        return_value=httpx.Response(302, headers={"location": "/fine"}))
    respx.get("https://h.example/fine").mock(return_value=httpx.Response(200, text="done"))
    priv = respx.get("https://h.example/private/x").mock(return_value=httpx.Response(200, text="x"))
    with httpx.Client() as c:
        assert fetch_target(c, "https://h.example/go").status == "blocked"
        assert fetch_target(c, "https://h.example/ok").body == b"done"
    assert not priv.called


@respx.mock
def test_redirect_loop_capped():
    respx.get("https://h.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://h.example/loop").mock(
        return_value=httpx.Response(302, headers={"location": "/loop"}))
    with httpx.Client() as c:
        assert fetch_target(c, "https://h.example/loop").error == "too many redirects"


@respx.mock
def test_allow_listed_host_allowed(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "watch_allowed_hosts", "127.0.0.1, other.example")
    respx.get("http://127.0.0.1/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("http://127.0.0.1/p").mock(return_value=httpx.Response(200, text="in"))
    with httpx.Client() as c:
        r = fetch_target(c, "http://127.0.0.1/p")
    assert r.status == "ok" and r.body == b"in"
