import json
import shutil
from pathlib import Path

import httpx
import pytest
import respx

from app.connectors import imf_weo
from app.connectors.imf_weo import ImfWeo
from app.connectors.registry import CONNECTORS

FX = Path(__file__).parent / "fixtures" / "imf"
BASE = "https://www.imf.org/external/datamapper/api/v1"
CODES = ["NGDP_RPCH", "NGDPD", "PCPIPCH"]


def _load(name):
    return json.loads((FX / name).read_text())


def _raw(tmp_path, failed=()):
    data = {c: _load(f"{c}.json") for c in CODES if c not in failed}
    raw = tmp_path / "raw.json"
    raw.write_text(
        json.dumps({"indicators": _load("indicators.json")["indicators"], "data": data,
                    "failed": list(failed)})
    )
    return raw


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(imf_weo, "_sleep", lambda s: None)


def test_registered():
    assert CONNECTORS["imf-weo"].slug == "imf-weo"


def test_parse_rows(tmp_path):
    res = ImfWeo().parse(_raw(tmp_path))
    assert res.complete is True
    rows = list(res.rows)
    hit = [r for r in rows if r.indicator == "NGDP_RPCH" and r.country == "FRA" and r.period == "1980"]
    assert len(hit) == 1
    assert hit[0].value == 1.8 and hit[0].dataset == "WEO"
    assert res.vintage == "World Economic Outlook (October 2026)"
    by = {i.code: i for i in res.indicators}
    assert by["NGDP_RPCH"].name == "Real GDP growth"
    assert by["NGDPD"].unit == "Billions of U.S. dollars"


def test_null_values_skipped(tmp_path):
    raw = tmp_path / "r.json"
    raw.write_text(json.dumps({
        "indicators": {"X": {"label": "x", "dataset": "WEO", "source": "S"}, "": {"label": None}},
        "data": {"X": {"values": {"X": {"FRA": {"2000": None, "2001": 3}}}}, "Y": {}},
        "failed": [],
    }))
    res = ImfWeo().parse(raw)
    rows = list(res.rows)
    assert [(r.period, r.value) for r in rows] == [("2001", 3)]
    assert [i.code for i in res.indicators] == ["X"]


def test_failed_indicator_marks_incomplete(tmp_path):
    res = ImfWeo().parse(_raw(tmp_path, failed=["NGDPD"]))
    assert res.complete is False
    assert not any(r.indicator == "NGDPD" for r in res.rows)
    assert res.warnings == ["incomplete: 1 indicators failed: NGDPD"]


@respx.mock
def test_download_ok(tmp_path):
    respx.get(f"{BASE}/indicators").respond(json=_load("indicators.json"))
    for c in CODES:
        respx.get(f"{BASE}/{c}").respond(json=_load(f"{c}.json"))
    dest = tmp_path / "o.json"
    ImfWeo().download(httpx.Client(), dest)
    out = json.loads(dest.read_text())
    assert out["failed"] == [] and set(out["data"]) == set(CODES)
    assert ImfWeo().parse(dest).complete is True


@respx.mock
def test_download_retries_then_records_failure(tmp_path):
    respx.get(f"{BASE}/indicators").respond(json=_load("indicators.json"))
    respx.get(f"{BASE}/NGDP_RPCH").respond(json=_load("NGDP_RPCH.json"))
    respx.get(f"{BASE}/PCPIPCH").respond(json=_load("PCPIPCH.json"))
    bad = respx.get(f"{BASE}/NGDPD").respond(500)
    dest = tmp_path / "o.json"
    ImfWeo().download(httpx.Client(), dest)
    out = json.loads(dest.read_text())
    assert out["failed"] == ["NGDPD"]
    assert "NGDPD" not in out["data"] and "NGDP_RPCH" in out["data"]
    assert bad.call_count == 3
    assert ImfWeo().parse(dest).complete is False


@respx.mock
def test_retry_then_success(tmp_path):
    respx.get(f"{BASE}/indicators").respond(json=_load("indicators.json"))
    for c in ("NGDP_RPCH", "PCPIPCH"):
        respx.get(f"{BASE}/{c}").respond(json=_load(f"{c}.json"))
    respx.get(f"{BASE}/NGDPD").mock(side_effect=[
        httpx.Response(503), httpx.ConnectError("x"), httpx.Response(200, json=_load("NGDPD.json"))])
    dest = tmp_path / "o.json"
    ImfWeo().download(httpx.Client(), dest)
    assert json.loads(dest.read_text())["failed"] == []


@respx.mock
def test_client_error_not_retried_but_recorded(tmp_path):
    respx.get(f"{BASE}/indicators").respond(json=_load("indicators.json"))
    for c in ("NGDP_RPCH", "PCPIPCH"):
        respx.get(f"{BASE}/{c}").respond(json=_load(f"{c}.json"))
    r = respx.get(f"{BASE}/NGDPD").respond(404)
    dest = tmp_path / "o.json"
    ImfWeo().download(httpx.Client(), dest)
    assert json.loads(dest.read_text())["failed"] == ["NGDPD"]
    assert r.call_count == 1


@respx.mock
def test_indicator_list_failure_raises(tmp_path):
    respx.get(f"{BASE}/indicators").respond(500)
    with pytest.raises(httpx.HTTPStatusError):
        ImfWeo().download(httpx.Client(), tmp_path / "o.json")
    assert not (tmp_path / "o.json").exists()


@respx.mock
def test_rate_limited(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(imf_weo, "_sleep", sleeps.append)
    respx.get(f"{BASE}/indicators").respond(json=_load("indicators.json"))
    for c in CODES:
        respx.get(f"{BASE}/{c}").respond(json=_load(f"{c}.json"))
    ImfWeo().download(httpx.Client(), tmp_path / "o.json")
    assert len(sleeps) >= 3 and all(s <= 0.5 for s in sleeps)


@respx.mock
def test_429_then_200_succeeds_and_honours_retry_after(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(imf_weo, "_sleep", sleeps.append)
    respx.get(f"{BASE}/indicators").respond(json=_load("indicators.json"))
    for c in ("NGDP_RPCH", "PCPIPCH"):
        respx.get(f"{BASE}/{c}").respond(json=_load(f"{c}.json"))
    respx.get(f"{BASE}/NGDPD").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "999"}),
        httpx.Response(200, json=_load("NGDPD.json"))])
    dest = tmp_path / "o.json"
    ImfWeo().download(httpx.Client(), dest)
    assert json.loads(dest.read_text())["failed"] == []
    assert 30.0 in sleeps and max(sleeps) == 30.0
