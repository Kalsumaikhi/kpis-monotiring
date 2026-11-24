"""World Bank World Development Indicators (WDI) bulk CSV connector.

Verified 2026-11-24:
- https://databank.worldbank.org/data/download/WDI_CSV.zip redirects (301) to
  https://databankfiles.worldbank.org/public/ddpext_download/WDI_CSV.zip (~282 MB), which is
  the URL used here (redirects are followed anyway).
- WDI data is published under CC BY 4.0; bulk download is the intended access path.
- Zip members: WDICSV.csv (data), WDISeries.csv (indicator metadata), WDICountry.csv,
  WDIcountry-series.csv, WDIfootnote.csv, WDIseries-time.csv. Only the first two are read.
- Both CSVs are UTF-8 with a BOM and CRLF line endings.
- WDICSV.csv header: Country Name, Country Code, Indicator Name, Indicator Code, 1960 ... 2025
  (one column per year; empty cell = no value).
- WDISeries.csv header starts: Series Code, Topic, Indicator Name, Short definition,
  Long definition, Unit of measure, ... (Unit of measure is mostly empty).
Aggregates and regions (e.g. AFE, WLD) are kept as-is.
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pandas as pd

from app.core.canonical import RawRow
from app.connectors.base import IndicatorMeta, ParseResult

URL = "https://databankfiles.worldbank.org/public/ddpext_download/WDI_CSV.zip"
DATA_FILE = "WDICSV.csv"
SERIES_FILE = "WDISeries.csv"
ID_COLS = ["Country Name", "Country Code", "Indicator Name", "Indicator Code"]
SERIES_COLS = ["Series Code", "Indicator Name"]
CHUNKSIZE = 50_000


def _clean(v) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


class WorldBankWDI:
    slug = "worldbank-wdi"
    name = "World Bank WDI"
    area = "economic"
    access = "api"
    raw_suffix = ".zip"

    def download(self, client: httpx.Client, dest: Path) -> None:
        with client.stream("GET", URL, follow_redirects=True) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes(1 << 20):
                    f.write(chunk)

    def parse(self, raw: Path) -> ParseResult:
        with zipfile.ZipFile(raw) as z:
            bad = z.testzip()
            if bad is not None:
                raise ValueError(f"corrupt member in WDI zip: {bad}")
            names = set(z.namelist())
            for n in (DATA_FILE, SERIES_FILE):
                if n not in names:
                    raise ValueError(f"WDI zip lacks {n}")
            with z.open(DATA_FILE) as f:
                header = pd.read_csv(f, encoding="utf-8-sig", nrows=0).columns.tolist()
            years = [c for c in header if c.isdigit()]
            missing = [c for c in ID_COLS if c not in header]
            if missing or not years:
                raise ValueError(f"WDI data CSV missing columns: {missing or 'year columns'}")
            with z.open(SERIES_FILE) as f:
                series = pd.read_csv(f, encoding="utf-8-sig", dtype=str)
            miss = [c for c in SERIES_COLS if c not in series.columns]
            if miss:
                raise ValueError(f"WDI series CSV missing columns: {miss}")
        indicators = [
            IndicatorMeta(
                code=r["Series Code"],
                name=r["Indicator Name"],
                unit=_clean(r.get("Unit of measure")),
                description=_clean(r.get("Short definition")) or _clean(r.get("Long definition")),
            )
            for r in series.to_dict("records")
        ]
        return ParseResult(
            rows=self._rows(raw, years), complete=True, indicators=indicators, vintage=None
        )

    def _rows(self, raw: Path, years: list[str]) -> Iterator[RawRow]:
        with zipfile.ZipFile(raw) as z, z.open(DATA_FILE) as f:
            reader = pd.read_csv(
                f,
                encoding="utf-8-sig",
                dtype=str,
                usecols=["Country Code", "Indicator Code", *years],
                chunksize=CHUNKSIZE,
            )
            for chunk in reader:
                long = chunk.melt(
                    id_vars=["Country Code", "Indicator Code"],
                    value_vars=years,
                    var_name="year",
                    value_name="value",
                ).dropna(subset=["value"])
                for country, indicator, year, value in long.itertuples(index=False):
                    yield RawRow(
                        dataset="WDI",
                        indicator=indicator,
                        country=country,
                        period=year,
                        value=value,
                    )
