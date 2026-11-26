"""A tiny threaded HTTPS login site for the login-engine tests (test helper, not a test module).

Served on 127.0.0.1 with a throwaway self-signed certificate for `localhost` and `127.0.0.1`;
the browser is told to trust exactly that certificate's key (see `spki`). Pages:

- GET /login            form: email + password (+ noise detection must skip); an <img> from the
                        second host name (127.0.0.1) that sets a cross-site cookie
- GET /login-custom     a form detection cannot handle (username after the password, no submit
                        button); needs explicit selectors
- POST /login           good credentials: code mode -> 303 /verify (code written to `code_file`),
                        else session cookie + 303 /data. Bad ones re-render the form.
- GET/POST /verify      the email-code step
- GET /data             protected content, else 302 /login
- GET /inline           protected content, else a login form in place (no redirect)
- GET /echo             protected page that echoes the password (for redaction tests)
- GET /pixel            sets the `tracker` cookie on whatever host served it
- GET /login-redirect   302 to /login on the second host name (127.0.0.1)
- GET /login-offsite    a form whose action posts to the second host name
- GET/POST /login-promo a form with an extra "promo code" box; bad logins re-render it here
- GET /api/data         JSON behind the session cookie, else 401 (JSON error)
- GET /api/json         JSON behind the session cookie, else 302 /login
- GET /promo-redirect   302 to /login-promo on the same host (the login page is not login_url)
- GET/POST /verify-unnamed  a code step whose box has no name or id (when `unnamed_code`);
                        after the code it lands back on the same path, with an unnamed search box
- GET /welcome          post-code landing page (when `pin_after_code`) with an unrelated
                        "pin" search box
`expire_all()` drops every session. `cookies_seen` records (host, Cookie header) per GET;
`posts` records (host, path, body) per POST.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import http.server
import ipaddress
import json
import secrets
import ssl
import tempfile
import threading
from pathlib import Path
from urllib.parse import parse_qs

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

PROTECTED = "PROTECTED DATA 42"


def _make_cert(directory: Path) -> tuple[Path, Path, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "site.crt", directory / "site.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    spki = key.public_key().public_bytes(serialization.Encoding.DER,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)
    return cert_path, key_path, base64.b64encode(hashlib.sha256(spki).digest()).decode()


LOGIN_FORM = """<html><body>
<input type="search" name="q" placeholder="Search the site">
<form method="post" action="/login">
  <input type="hidden" name="csrf" value="x">
  <input type="email" name="email">
  <input type="text" name="nickname" style="display:none">
  <input type="password" name="password">
  <button type="button">Show</button>
  <button type="submit">Sign in</button>
</form>
{error}
<img src="{pixel}">
</body></html>"""

CUSTOM_FORM = """<html><body>
<form method="post" action="/login" id="f">
  <input type="password" id="pw" name="password">
  <input type="text" id="who" name="email">
  <a href="#" id="go" onclick="document.getElementById('f').submit(); return false">Go</a>
</form>
{error}
</body></html>"""

OFFSITE_FORM = """<html><body>
<form method="post" action="{action}">
  <input type="email" name="email">
  <input type="password" name="password">
  <button type="submit">Sign in</button>
</form>
</body></html>"""

PROMO_FORM = """<html><body>
<form method="post" action="/login-promo">
  <input type="email" name="email">
  <input type="password" name="password">
  <input type="text" name="promo_code" placeholder="Promo code">
  <button type="submit">Sign in</button>
</form>
{error}
</body></html>"""

VERIFY_FORM = """<html><body>
<p>We emailed you a code.</p>
<form method="post" action="/verify">
  <input type="text" name="otp" inputmode="numeric" maxlength="6">
  <button type="submit">Verify</button>
</form>
{error}
</body></html>"""


UNNAMED_VERIFY_FORM = """<html><body>
<p>We emailed you a code.</p>
<form method="post" action="/verify-unnamed"
      onsubmit="this.otp.value = this.querySelector('[autocomplete]').value">
  <input type="hidden" name="otp">
  <input type="text" inputmode="numeric" maxlength="6" autocomplete="one-time-code">
  <button type="submit">Verify</button>
</form>
{error}
</body></html>"""


class LoginSite:
    def __init__(self, username: str, password: str, code_file: Path | None = None) -> None:
        self.username = username
        self.password = password
        self.code_file = code_file
        self.code_mode = False
        self.pin_after_code = False
        self.unnamed_code = False
        self.posts: list[tuple[str, str, str]] = []
        self.sessions: set[str] = set()
        self.pending: dict[str, str] = {}  # pending token -> code
        self.cookies_seen: list[tuple[str, str]] = []
        self._tmp = tempfile.TemporaryDirectory()
        cert, key, self.spki = _make_cert(Path(self._tmp.name))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._srv.daemon_threads = True
        self._srv.socket = ctx.wrap_socket(self._srv.socket, server_side=True)
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    def url(self, path: str, host: str = "localhost") -> str:
        return f"https://{host}:{self.port}{path}"

    def expire_all(self) -> None:
        self.sessions.clear()

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._tmp.cleanup()

    def _handler(self):
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _cookies(self) -> dict[str, str]:
                out = {}
                for part in (self.headers.get("Cookie") or "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k:
                        out[k] = v
                return out

            def _authed(self) -> bool:
                return self._cookies().get("sid") in site.sessions

            def _send(self, code: int, body: str = "", headers: dict | None = None) -> None:
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _login_page(self, error: str = "") -> None:
                pixel = site.url("/pixel", host="127.0.0.1")
                self._send(200, LOGIN_FORM.format(error=error, pixel=pixel))

            def _sign_in_cookie(self) -> str:
                sid = secrets.token_hex(16)
                site.sessions.add(sid)
                return f"sid={sid}; Path=/; Secure; HttpOnly"

            def do_GET(self):
                host = (self.headers.get("Host") or "").split(":")[0]
                site.cookies_seen.append((host, self.headers.get("Cookie") or ""))
                path = self.path.split("?")[0]
                if path == "/robots.txt":
                    self._send(404)
                elif path == "/login":
                    self._login_page()
                elif path == "/login-custom":
                    self._send(200, CUSTOM_FORM.format(error=""))
                elif path == "/verify":
                    self._send(200, VERIFY_FORM.format(error=""))
                elif path == "/login-redirect":
                    self._send(302, "", {"Location": site.url("/login", host="127.0.0.1")})
                elif path == "/login-offsite":
                    self._send(200, OFFSITE_FORM.format(
                        action=site.url("/login", host="127.0.0.1")))
                elif path == "/login-promo":
                    self._send(200, PROMO_FORM.format(error=""))
                elif path == "/promo-redirect":
                    self._send(302, "", {"Location": "/login-promo"})
                elif path == "/verify-unnamed":
                    if self._authed():
                        self._send(200, f"<html><body><main>{PROTECTED}</main>"
                                        '<input type="search" placeholder="Search">'
                                        "</body></html>")
                    else:
                        self._send(200, UNNAMED_VERIFY_FORM.format(error=""))
                elif path == "/welcome" and self._authed():
                    self._send(200, f"<html><body><main>{PROTECTED}</main>"
                                    '<input type="text" name="pin_search" placeholder="Find a PIN">'
                                    "</body></html>")
                elif path == "/pixel":
                    self._send(200, "", {"Set-Cookie":
                                         "tracker=t1; Path=/; Secure; SameSite=None"})
                elif path in ("/api/data", "/api/json"):
                    if self._authed():
                        data = json.dumps({"value": 42, "label": PROTECTED,
                                           "who": site.password}).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                    elif path == "/api/data":
                        self._send(401, '{"error": "unauthorized"}')
                    else:
                        self._send(302, "", {"Location": "/login"})
                elif path in ("/data", "/inline", "/echo"):
                    if self._authed():
                        extra = f"<p>you are {site.password}</p>" if path == "/echo" else ""
                        self._send(200, f"<html><body><main>{PROTECTED}</main>{extra}"
                                        "</body></html>")
                    elif path == "/inline":
                        self._login_page()
                    else:
                        self._send(302, "", {"Location": "/login"})
                else:
                    self._send(404, "not found")

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode()
                host = (self.headers.get("Host") or "").split(":")[0]
                site.posts.append((host, self.path, raw))
                form = {k: v[0] for k, v in parse_qs(raw).items()}
                if self.path == "/login-promo":
                    ok = (form.get("email") == site.username
                          and form.get("password") == site.password)
                    if ok:
                        self._send(303, "", {"Location": "/data",
                                             "Set-Cookie": self._sign_in_cookie()})
                    else:
                        self._send(200, PROMO_FORM.format(error="<p>wrong password</p>"))
                    return
                if self.path == "/login":
                    if (form.get("email") != site.username
                            or form.get("password") != site.password):
                        # Echo the attempt back, as some sites do, to prove it is redacted.
                        self._login_page(f"<p>bad login for {form.get('password')}</p>")
                        return
                    if site.code_mode:
                        token = secrets.token_hex(8)
                        code = f"{secrets.randbelow(10 ** 6):06d}"
                        site.pending[token] = code
                        if site.code_file is not None:
                            site.code_file.write_text(code)
                        verify = "/verify-unnamed" if site.unnamed_code else "/verify"
                        self._send(303, "", {"Location": verify,
                                             "Set-Cookie": f"pending={token}; Path=/; Secure"})
                        return
                    self._send(303, "", {"Location": "/data", "Set-Cookie": self._sign_in_cookie()})
                elif self.path in ("/verify", "/verify-unnamed"):
                    token = self._cookies().get("pending", "")
                    if token in site.pending and form.get("otp") == site.pending[token]:
                        del site.pending[token]
                        landing = "/welcome" if site.pin_after_code else "/data"
                        if self.path == "/verify-unnamed":
                            landing = "/verify-unnamed"
                        self._send(303, "", {"Location": landing,
                                             "Set-Cookie": self._sign_in_cookie()})
                    else:
                        form_html = (UNNAMED_VERIFY_FORM if self.path == "/verify-unnamed"
                                     else VERIFY_FORM)
                        self._send(200, form_html.format(
                            error=f"<p>code {form.get('otp')} is wrong</p>"))
                else:
                    self._send(404, "not found")

        return Handler
