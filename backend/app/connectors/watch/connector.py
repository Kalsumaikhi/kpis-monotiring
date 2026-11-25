"""Connector for DB-defined 'watch' sources (watched web pages / API links)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import httpx

from app.connectors.base import ParseResult


class WatchConnector:
    area = "custom"
    access = "watch"
    raw_suffix = ".json"

    def __init__(self, source_id: int, slug: str, name: str, session_factory: Callable[[], Any]):
        self.source_id = source_id
        self.slug = slug
        self.name = name
        self.session_factory = session_factory

    def download(self, client: httpx.Client, dest: Path) -> None:
        raise NotImplementedError("watch connector arrives in Task 4")

    def parse(self, raw: Path) -> ParseResult:
        raise NotImplementedError("watch connector arrives in Task 4")
