import shutil
from pathlib import Path

import httpx
import openpyxl
import pytest
import respx

from app.connectors.registry import CONNECTORS
from app.connectors.sipri_milex import SipriMilex

FX = Path(__file__).parent / "fixtures" / "sipri" / "milex_sample.xlsx"
PAGE = "https://www.sipri.org/databases/milex"
CODES = {"constant-usd", "current-usd", "share-of-gdp", "per-capita", "share-of-govt-spending"}


def test_registered():
    assert CONNECTORS["sipri-milex"].slug == "sipri-milex"
    assert CONNECTORS["sipri-milex"].raw_suffix == ".xlsx"


def test_parse_all_sheets():
    res = SipriMilex().parse(FX)
    rows = list(res.rows)
    assert res.complete is True
    assert {i.code for i in res.indicators} == CODES
    assert {r.indicator for r in rows} == CODES
    names = {i.code: i.name for i in res.indicators}
    assert names["constant-usd"] == "Constant (2024) US$"
    assert names["share-of-govt-spending"] == "Share of Govt. spending"
    assert {r.dataset for r in rows} == {"SIPRI-MILEX"}


def test_known_value():
    rows = list(SipriMilex().parse(FX).rows)
    hit = [r for r in rows if r.indicator == "constant-usd" and r.country == "Algeria" and r.period == "2025"]
    assert len(hit) == 1 and hit[0].value == pytest.approx(24128.442879148966)
    assert not [r for r in rows if r.indicator == "per-capita" and r.period == "1987"]  # sheet starts 1988
    assert [r for r in rows if r.indicator == "per-capita" and r.period == "1988"]


def test_missing_markers_and_regions_skipped():
    rows = list(SipriMilex().parse(FX).rows)
    assert all(isinstance(r.value, (int, float)) for r in rows)
    assert {r.country for r in rows} == {"Algeria", "Libya", "Morocco"}  # no Africa / North Africa
    # Algeria 1949 is "xxx" in the constant sheet: no row
    assert not [r for r in rows if r.indicator == "constant-usd" and r.country == "Algeria" and r.period == "1949"]


def test_notes_rows_ignored():
    rows = list(SipriMilex().parse(FX).rows)
    assert not [r for r in rows if "Figures" in r.country or "Source" in r.country or r.country.startswith("†")]


def test_vintage():
    res = SipriMilex().parse(FX)
    assert res.vintage == "SIPRI Military Expenditure Database 2026 (data to 2025)"


def test_missing_sheet_marks_incomplete(tmp_path):
    wb = openpyxl.load_workbook(FX)
    del wb["Per capita"]
    p = tmp_path / "x.xlsx"
    wb.save(p)
    res = SipriMilex().parse(p)
    assert res.complete is False
    assert any("per-capita" in w for w in res.warnings)
    assert "per-capita" not in {r.indicator for r in res.rows}


def test_base_year_change_keeps_code(tmp_path):
    wb = openpyxl.load_workbook(FX)
    wb["Constant (2024) US$"].title = "Constant (2025) US$"
    p = tmp_path / "x.xlsx"
    wb.save(p)
    res = SipriMilex().parse(p)
    assert res.complete is True
    assert "constant-usd" in {i.code for i in res.indicators}


@respx.mock
def test_download_discovers_link(tmp_path):
    html = (
        '<a href="/sites/default/files/other.pdf">x</a>'
        '<a href="//www.sipri.org/sites/default/files/SIPRI-Milex-data-1949-2025_v1.2.xlsx">data</a>'
    )
    respx.get(PAGE).mock(return_value=httpx.Response(200, text=html))
    f = respx.get(
        "https://www.sipri.org/sites/default/files/SIPRI-Milex-data-1949-2025_v1.2.xlsx"
    ).mock(return_value=httpx.Response(200, content=FX.read_bytes()))
    dest = tmp_path / "raw.xlsx"
    with httpx.Client() as c:
        SipriMilex().download(c, dest)
    assert f.called
    assert dest.read_bytes() == FX.read_bytes()
    assert not (tmp_path / "raw.xlsx.part").exists()


@respx.mock
def test_download_no_link_raises(tmp_path):
    respx.get(PAGE).mock(return_value=httpx.Response(200, text="<html>nothing</html>"))
    dest = tmp_path / "raw.xlsx"
    with httpx.Client() as c, pytest.raises(RuntimeError, match="link"):
        SipriMilex().download(c, dest)
    assert not dest.exists()


@respx.mock
def test_download_http_error_raises(tmp_path):
    respx.get(PAGE).mock(return_value=httpx.Response(500))
    with httpx.Client() as c, pytest.raises(httpx.HTTPStatusError):
        SipriMilex().download(c, tmp_path / "raw.xlsx")
