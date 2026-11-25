"""In-process forward proxy that enforces the watch destination policy for headless Chromium.

Chromium is launched with ``--proxy-server`` pointing here (Playwright also removes the
implicit loopback bypass), so every request it makes, including redirect follow-ups,
subresources, HTTPS (CONNECT) and WebSockets (CONNECT), is checked against
``destination_allowed`` rules. The proxy resolves the host once and connects to the checked
address, so DNS rebinding cannot swap in an internal address after the check.
"""

from __future__ import annotations

import logging
import selectors
import socket
import socketserver
import threading
from urllib.parse import urlsplit

from app.connectors.watch import fetch as fetch_mod

log = logging.getLogger(__name__)
MAX_HEAD = 64 * 1024
IDLE_TIMEOUT = 30.0
HOP_HEADERS = {b"proxy-connection", b"proxy-authorization", b"connection", b"keep-alive"}


def _split_hostport(target: str, default_port: int) -> tuple[str, int] | None:
    if target.startswith("["):
        host, _, rest = target[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    else:
        host, _, port = target.rpartition(":") if ":" in target else (target, "", "")
    if not host:
        return None
    try:
        return host, int(port) if port else default_port
    except ValueError:
        return None


class _Handler(socketserver.BaseRequestHandler):
    server: _Server

    def handle(self) -> None:
        client: socket.socket = self.request
        client.settimeout(IDLE_TIMEOUT)
        head = b""
        try:
            while b"\r\n\r\n" not in head:
                chunk = client.recv(8192)
                if not chunk:
                    return
                head += chunk
                if len(head) > MAX_HEAD:
                    return
        except OSError:
            return
        head, _, rest = head.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        try:
            method, target, version = lines[0].decode("latin-1").split(" ", 2)
        except ValueError:
            self._reply(client, 400)
            return
        if method == "CONNECT":
            hp = _split_hostport(target, 443)
            if hp is None:
                self._reply(client, 400)
                return
            upstream = self.server.policy_connect(*hp)
            if upstream is None:
                self._reply(client, 403)
                return
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            first = rest
        else:
            parts = urlsplit(target)
            if parts.scheme != "http" or not parts.hostname:
                self._reply(client, 400)
                return
            try:
                port = parts.port or 80
            except ValueError:
                self._reply(client, 400)
                return
            upstream = self.server.policy_connect(parts.hostname, port)
            if upstream is None:
                self._reply(client, 403)
                return
            path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            out = [f"{method} {path} {version}".encode("latin-1")]
            for line in lines[1:]:
                name = line.split(b":", 1)[0].strip().lower()
                if name not in HOP_HEADERS:
                    out.append(line)
            out.append(b"Connection: close")
            first = b"\r\n".join(out) + b"\r\n\r\n" + rest
        try:
            if first:
                upstream.sendall(first)
            self._relay(client, upstream)
        except OSError:
            pass
        finally:
            upstream.close()

    @staticmethod
    def _reply(client: socket.socket, code: int) -> None:
        reason = {400: "Bad Request", 403: "Forbidden"}[code]
        try:
            client.sendall(f"HTTP/1.1 {code} {reason}\r\nContent-Length: 0\r\n"
                           f"Connection: close\r\n\r\n".encode())
        except OSError:
            pass

    @staticmethod
    def _relay(a: socket.socket, b: socket.socket) -> None:
        sel = selectors.DefaultSelector()
        sel.register(a, selectors.EVENT_READ, b)
        sel.register(b, selectors.EVENT_READ, a)
        try:
            while True:
                events = sel.select(IDLE_TIMEOUT)
                if not events:
                    return
                for key, _ in events:
                    data = key.fileobj.recv(65536)
                    if not data:
                        return
                    key.data.sendall(data)
        finally:
            sel.close()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.blocked: list[str] = []

    def policy_connect(self, host: str, port: int) -> socket.socket | None:
        addrs = fetch_mod.checked_addresses(host)
        if not addrs:
            self.blocked.append(f"{host}:{port}")
            log.info("watch browser: blocked request to %s:%s", host, port)
            return None
        for addr in addrs:
            try:
                return socket.create_connection((addr, port), timeout=fetch_mod.TIMEOUT)
            except OSError:
                continue
        return None


class PolicyProxy:
    """Starts on construction; `url` is what Chromium's proxy setting needs. Call close()."""

    def __init__(self) -> None:
        self._server = _Server()
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address[:2]
        self.url = f"http://{host}:{port}"

    @property
    def blocked(self) -> list[str]:
        return self._server.blocked

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
