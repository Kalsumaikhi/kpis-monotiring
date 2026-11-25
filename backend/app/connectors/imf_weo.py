"""IMF DataMapper connector (World Economic Outlook and sibling IMF datasets).

Verified 2026-11-24 against the live API:
- GET https://www.imf.org/external/datamapper/api/v1/indicators returns
  {"indicators": {CODE: {"label", "description", "source", "unit", "dataset", "last-modified"}}}
  (132 indicators across datasets WEO, AFRREO, CF, CL, SPRLU, GDD, FPP, FM, ...). Fields can be
  null/empty and a blank code key can occur; blank codes are skipped.
- GET .../api/v1/{CODE} returns {"values": {CODE: {ISO3: {"1980": 2.5, ...}}}}. "values" (or
  values[code]) can be missing or empty on a 200; since a listed indicator should have data, that
  is treated as a failed indicator (transient empty responses must not read as mass removals).
  Null values are skipped.
- Country keys include aggregates (regions/groups); kept as-is.
- `source` looks like "World Economic Outlook (October 2026)" and is the only release label the
  API reports.

Design choices:
- All indicators in the list are fetched, not only WEO. RawRow.dataset is the indicator's
  `dataset` field (fallback "IMF").
- vintage = the most common non-empty `source` among indicators whose dataset == "WEO"; None if
  there are none.
- At most 2 requests/second (0.5 s pause between requests). Each indicator request is tried up
  to 3 times with exponential backoff on network errors, 5xx, 408 and 429 (a numeric Retry-After
  is honoured, capped at 30 s); other 4xx are not retried. A code that
  still fails is recorded under "failed" in the raw file and parse returns complete=False.
  A failure to fetch the indicator list raises.
- Raw file: {"indicators": {...}, "data": {code: payload}, "failed": [code, ...]}.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import httpx

from app.connectors.base import IndicatorMeta, ParseResult
from app.core.canonical import RawRow

BASE = "https://www.imf.org/external/datamapper/api/v1"
MIN_INTERVAL = 0.5  # seconds between requests => max 2 requests/second
ATTEMPTS = 3
BACKOFF = 1.0  # seconds; doubled per retry
TIMEOUT = 60.0
RETRYABLE = {408, 429}
MAX_RETRY_AFTER = 30.0


def _sleep(seconds: float) -> None:  # module-level so tests can patch it
    time.sleep(seconds)


def _clean(v) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


class ImfWeo:
    slug = "imf-weo"
    attribution = "Source: IMF DataMapper"
    name = "IMF World Economic Outlook"
    area = "economic"
    access = "api"
    raw_suffix = ".json"

    def download(self, client: httpx.Client, dest: Path) -> None:
        state = {"first": True}
        resp = self._get(client, f"{BASE}/indicators", state)
        indicators = resp.json().get("indicators") or {}
        data: dict[str, dict] = {}
        failed: list[str] = []
        for code in indicators:
            if not code or not code.strip():
                continue
            try:
                data[code] = self._get(client, f"{BASE}/{code}", state).json()
            except (httpx.HTTPError, ValueError):
                failed.append(code)
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_text(json.dumps({"indicators": indicators, "data": data, "failed": failed}))
        tmp.replace(dest)

    def _get(self, client: httpx.Client, url: str, state: dict) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(ATTEMPTS):
            if not state["first"]:
                _sleep(MIN_INTERVAL)
            state["first"] = False
            delay = BACKOFF * 2**attempt
            try:
                r = client.get(url, timeout=TIMEOUT)
                if r.status_code < 500 and r.status_code not in RETRYABLE:
                    r.raise_for_status()  # other 4xx: raise immediately, no retry
                    return r
                last = httpx.HTTPStatusError(f"{r.status_code} for {url}", request=r.request, response=r)
                ra = r.headers.get("Retry-After", "")
                if ra.strip().isdigit():
                    delay = min(float(ra), MAX_RETRY_AFTER)
            except httpx.HTTPStatusError:
                raise
            except httpx.TransportError as e:
                last = e
            if attempt < ATTEMPTS - 1:
                _sleep(delay)
        assert last is not None
        raise last

    def parse(self, raw: Path) -> ParseResult:
        doc = json.loads(raw.read_text())
        indicators: dict = doc["indicators"]
        data: dict = doc["data"]
        failed = list(doc.get("failed") or [])
        for code in indicators:
            if not code or not code.strip() or code in failed:
                continue
            by_country = (((data.get(code) or {}).get("values") or {}).get(code))
            if not by_country:
                failed.append(code)
        metas = [
            IndicatorMeta(
                code=code,
                name=_clean(m.get("label")) or code,
                unit=_clean(m.get("unit")),
                description=_clean(m.get("description")),
            )
            for code, m in indicators.items()
            if code and code.strip()
        ]
        sources = Counter(
            s
            for m in indicators.values()
            if m.get("dataset") == "WEO" and (s := _clean(m.get("source")))
        )
        vintage = sources.most_common(1)[0][0] if sources else None
        warnings = []
        if failed:
            listed = ", ".join(failed[:10]) + (", ..." if len(failed) > 10 else "")
            warnings.append(f"incomplete: {len(failed)} indicators failed: {listed}")
        return ParseResult(
            rows=self._rows(indicators, data), complete=not failed, indicators=metas, vintage=vintage,
            warnings=warnings,
        )

    def _rows(self, indicators: dict, data: dict) -> Iterator[RawRow]:
        for code, payload in data.items():
            dataset = _clean((indicators.get(code) or {}).get("dataset")) or "IMF"
            by_country = ((payload or {}).get("values") or {}).get(code) or {}
            for country, series in by_country.items():
                for period, value in (series or {}).items():
                    if value is None:
                        continue
                    yield RawRow(dataset=dataset, indicator=code, country=country, period=period, value=value)
