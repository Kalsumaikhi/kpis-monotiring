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


class Connector(Protocol):
    slug: str
    name: str
    area: str
    access: str
    raw_suffix: str  # required: extension of the single raw file, e.g. ".zip"

    def download(self, client: httpx.Client, dest: Path) -> None:
        """Write exactly one raw file at `dest`."""

    def parse(self, raw: Path) -> ParseResult: ...
