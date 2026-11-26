"""Connector contract: download one raw file, parse it into raw rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

import httpx

from app.core.canonical import RawRow


@dataclass
class IndicatorMeta:
    code: str
    name: str
    unit: str | None = None
    description: str | None = None


@dataclass
class ParseResult:
    rows: Iterable[RawRow]
    complete: bool
    indicators: list[IndicatorMeta]
    vintage: str | None = None
    warnings: list[str] = field(default_factory=list)
    # Indicators whose rows are stored silently this run (no counts, no changes).
    baseline_indicators: list[str] = field(default_factory=list)


class Connector(Protocol):
    slug: str
    name: str
    area: str
    access: str
    raw_suffix: str  # required: extension of the single raw file, e.g. ".zip"
    # Optional: `credential_hosts: tuple[str, ...] = ()`. Lowercase hostnames the connector
    # may send a stored key to. When the source has a key (Source.credential_id) the pipeline
    # adds it, via an httpx request hook on the client passed to download(), to https requests
    # (including redirect hops) whose host is listed; other hosts and http never get it.
    # Absent or empty means the source does not support keys and a stored key is ignored.
    # A connector that declares credential_hosts must never log, raise, or write into its raw
    # file the raw text of an httpx exception, an httpx.Request/Response repr, or a request
    # URL (str(request.url), response.url): once the hook has run they carry the key (a
    # query key in the URL, a header key in the request). Log or raise with
    # type(e).__name__ and a status code instead. The pipeline scrubs run.error and the raw
    # file as a backstop, but logs and anything else the connector emits are not scrubbed.

    def download(self, client: httpx.Client, dest: Path) -> None:
        """Write exactly one raw file at `dest`."""

    def parse(self, raw: Path) -> ParseResult: ...
