"""Encrypted key store: Fernet helpers and applying a stored key to an outgoing request.

KPI_SECRET_KEY is read fresh on every call (a new Settings(), which reads the environment and
.env) rather than through the cached get_settings(), so tests can monkeypatch the environment.
A valid Fernet key is used as is; any other value is derived with SHA-256.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass, field
from urllib.parse import (
    parse_qsl, quote, quote_plus, unquote, unquote_plus, urlencode, urlsplit, urlunsplit,
)

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Credential as CredentialRow


class KeyUnavailable(Exception):
    pass


def get_fernet() -> Fernet:
    """Build the Fernet for the current KPI_SECRET_KEY (build once per request and pass to decrypt)."""
    secret = Settings().kpi_secret_key
    value = secret.get_secret_value() if secret else ""
    if not value:
        raise KeyUnavailable("KPI_SECRET_KEY not set")
    try:
        if len(base64.urlsafe_b64decode(value.encode())) == 32:
            return Fernet(value.encode())
    except (binascii.Error, ValueError):
        pass
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest()))


def encrypt(secret: str) -> bytes:
    return get_fernet().encrypt(secret.encode())


def decrypt(blob: bytes, fernet: Fernet | None = None) -> str:
    f = fernet or get_fernet()
    try:
        return f.decrypt(bytes(blob)).decode()
    except InvalidToken:
        raise KeyUnavailable("cannot decrypt — was KPI_SECRET_KEY changed?") from None


@dataclass
class Credential:
    id: int
    name: str
    kind: str
    header_name: str | None
    prefix: str | None
    param_name: str | None
    username: str | None
    secret: str = field(repr=False)


def load(session: Session, credential_id: int) -> Credential:
    row = session.get(CredentialRow, credential_id)
    if row is None:
        raise KeyError(credential_id)
    return Credential(row.id, row.name, row.kind, row.header_name, row.prefix,
                      row.param_name, row.username, decrypt(row.secret_enc))


def _basic_token(cred: Credential) -> str:
    return base64.b64encode(f"{cred.username or ''}:{cred.secret}".encode()).decode()


def apply(cred: Credential, url: str, headers: dict) -> tuple[str, dict]:
    headers = dict(headers)
    if cred.kind == "query":
        parts = urlsplit(url)
        name = cred.param_name or ""
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != name]
        query.append((name, cred.secret))
        url = urlunsplit(parts._replace(query=urlencode(query)))
    elif cred.kind == "basic":
        headers["Authorization"] = f"Basic {_basic_token(cred)}"
    else:
        value = f"{cred.prefix} {cred.secret}" if cred.prefix else cred.secret
        headers[cred.header_name or "Authorization"] = value
    return url, headers


def strip_query_secret(cred: Credential | None, url: str) -> str:
    """`url` without any `param_name=<secret>` pair of a query key (value compared decoded,
    with and without '+' as space); other parameters keep their original spelling. Used on
    every redirect hop so a Location that echoes the key is not followed as-is to a host
    that must not get it; apply() adds the key back on hops that may carry it."""
    if cred is None or cred.kind != "query" or not cred.param_name or not cred.secret:
        return url
    parts = urlsplit(url)
    if not parts.query:
        return url

    def carries_secret(seg: str) -> bool:
        k, _, v = seg.partition("=")
        return (unquote_plus(k) == cred.param_name
                and cred.secret in (unquote(v), unquote_plus(v)))

    segs = parts.query.split("&")
    kept = [s for s in segs if not carries_secret(s)]
    if len(kept) == len(segs):
        return url
    return urlunsplit(parts._replace(query="&".join(kept)))


_HEX = "0123456789abcdefABCDEF"


def _pattern(form: str) -> str:
    """Regex for `form` where the hex digits of a %XX escape match in either case."""
    out, i = [], 0
    while i < len(form):
        if form[i] == "%" and len(form) >= i + 3 and all(c in _HEX for c in form[i + 1:i + 3]):
            out.append("%" + "".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else c
                                     for c in form[i + 1:i + 3]))
            i += 3
        else:
            out.append(re.escape(form[i]))
            i += 1
    return "".join(out)


def _forms(cred: Credential) -> list[str]:
    """Every spelling of the secret (and for basic auth the Authorization token) to scrub,
    longest first."""
    values = [cred.secret] if cred.secret else []
    if cred.kind == "basic":
        values.append(_basic_token(cred))
    forms = set()
    for v in values:
        js = json.dumps(v)[1:-1]  # as it appears inside a JSON string
        forms |= {v, quote_plus(v), quote(v, safe=""), quote(v), js, js.replace("/", "\\/"),
                  json.dumps(v, ensure_ascii=False)[1:-1]}
    return sorted(forms, key=len, reverse=True)


def _scrubber(cred: Credential, encoding: str | None = None):
    forms = _forms(cred)
    if encoding:  # match the UTF-8 bytes of a form as seen through a latin-1 decode
        forms = [f.encode().decode(encoding) for f in forms]
    return re.compile("|".join(_pattern(f) for f in forms)) if forms else None


def redact(text: str, cred: Credential | None) -> str:
    if cred is None or not text:
        return text
    rx = _scrubber(cred)
    return rx.sub("***", text) if rx else text


def redact_bytes(data: bytes, cred: Credential | None) -> bytes:
    """redact() over a response body of unknown charset (matches the UTF-8 form of the secret)."""
    if cred is None or not data:
        return data
    rx = _scrubber(cred, "latin-1")
    return rx.sub("***", data.decode("latin-1")).encode("latin-1") if rx else data


def mark_used(session: Session, credential_id: int, error: str | None) -> None:
    """Record a use. Runs in its own short session on the same bind, so it never commits the
    caller's session; updated_at is left untouched (it tracks edits, not use)."""
    with Session(bind=session.get_bind(), join_transaction_mode="create_savepoint") as s:
        s.execute(
            update(CredentialRow).where(CredentialRow.id == credential_id)
            .values(last_used_at=func.now(), last_error=error, updated_at=CredentialRow.updated_at)
        )
        s.commit()
