import shutil
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from app.connectors.registry import CONNECTORS
from app.connectors.worldbank_wdi import WorldBankWDI

FIXTURE = Path(__file__).parent / "fixtures" / "wdi" / "WDI_CSV_sample.zip"


def _expected_cells():
    with zipfile.ZipFile(FIXTURE) as z, z.open("WDICSV.csv") as f:
        df = pd.read_csv(f, encoding="utf-8-sig", dtype=str)
    years = [c for c in df.columns if c.isdigit()]
    return int(df[years].notna().sum().sum())


def test_registered():
    assert CONNECTORS["worldbank-wdi"].slug == "worldbank-wdi"


def test_parse_counts_and_keys():
    res = WorldBankWDI().parse(FIXTURE)
    assert res.complete is True
    rows = list(res.rows)
    assert len(rows) == _expected_cells() > 0
    hit = [
        r for r in rows
        if r.indicator == "NY.GDP.MKTP.CD" and r.country == "FRA" and r.period == "2020"
    ]
    assert len(hit) == 1
    assert hit[0].dataset == "WDI"
    assert float(hit[0].value) == pytest.approx(2.6479e12, rel=1e-3)


def test_indicator_metadata():
    res = WorldBankWDI().parse(FIXTURE)
    by_code = {i.code: i for i in res.indicators}
    assert len(by_code) == 5
    assert by_code["NY.GDP.MKTP.CD"].name.startswith("GDP")


def test_truncated_zip_fails(tmp_path):
    bad = tmp_path / "trunc.zip"
    data = FIXTURE.read_bytes()
    bad.write_bytes(data[: len(data) // 2])
    with pytest.raises(Exception):
        res = WorldBankWDI().parse(bad)
        list(res.rows)


def test_missing_column_raises(tmp_path):
    bad = tmp_path / "nocol.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("WDICSV.csv", "Country Name,Country Code\r\nFrance,FRA\r\n")
        z.writestr("WDISeries.csv", "Series Code,Indicator Name\r\n")
    with pytest.raises(ValueError):
        WorldBankWDI().parse(bad)
