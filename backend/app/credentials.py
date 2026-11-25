"""Encrypted key store: Fernet helpers and applying a stored key to an outgoing request.

KPI_SECRET_KEY is read fresh on every call (a new Settings(), which reads the environment and
.env) rather than through the cached get_settings(), so tests can monkeypatch the environment.
A valid Fernet key is used as is; any other value is derived with SHA-256.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Credential as CredentialRow


class KeyUnavailable(Exception):
    pass


def _fernet() -> Fernet:
    value = Settings().kpi_secret_key
    if not value:
        raise KeyUnavailable("KPI_SECRET_KEY not set")
    try:
        if len(base64.urlsafe_b64decode(value.encode())) == 32:
            return Fernet(value.encode())
    except (binascii.Error, ValueError):
        pass
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest()))


def encrypt(secret: str) -> bytes:
    return _fernet().encrypt(secret.encode())


def decrypt(blob: bytes) -> str:
    f = _fernet()
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
    secret: str


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
        query = parse_qsl(parts.query, keep_blank_values=True) + [(cred.param_name or "", cred.secret)]
        url = urlunsplit(parts._replace(query=urlencode(query)))
    elif cred.kind == "basic":
        headers["Authorization"] = f"Basic {_basic_token(cred)}"
    else:
        value = f"{cred.prefix} {cred.secret}" if cred.prefix else cred.secret
        headers[cred.header_name or "Authorization"] = value
    return url, headers


def redact(text: str, cred: Credential | None) -> str:
    if cred is None or not text:
        return text
    if cred.kind == "basic":
        text = text.replace(_basic_token(cred), "***")
    if cred.secret:
        text = text.replace(cred.secret, "***")
    return text


def mark_used(session: Session, credential_id: int, error: str | None) -> None:
    session.execute(
        update(CredentialRow).where(CredentialRow.id == credential_id)
        .values(last_used_at=func.now(), last_error=error)
    )
    session.commit()
