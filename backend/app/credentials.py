"""Encrypted key store: Fernet helpers and applying a stored key to an outgoing request.

KPI_SECRET_KEY is read fresh on every call (a new Settings(), which reads the environment and
.env) rather than through the cached get_settings(), so tests can monkeypatch the environment.
A valid Fernet key is used as is; any other value is derived with SHA-256.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit

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


def redact(text: str, cred: Credential | None) -> str:
    if cred is None or not text:
        return text
    if cred.kind == "basic":
        text = text.replace(_basic_token(cred), "***")
    if cred.secret:
        forms = {cred.secret, quote_plus(cred.secret), quote(cred.secret, safe=""), quote(cred.secret)}
        for form in sorted(forms, key=len, reverse=True):
            text = text.replace(form, "***")
    return text


def mark_used(session: Session, credential_id: int, error: str | None) -> None:
    """Record a use. Runs in its own short session on the same bind, so it never commits the
    caller's session; updated_at is left untouched (it tracks edits, not use)."""
    with Session(bind=session.get_bind(), join_transaction_mode="create_savepoint") as s:
        s.execute(
            update(CredentialRow).where(CredentialRow.id == credential_id)
            .values(last_used_at=func.now(), last_error=error, updated_at=CredentialRow.updated_at)
        )
        s.commit()
