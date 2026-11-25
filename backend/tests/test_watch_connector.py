import json

import httpx
import pytest
import respx
from sqlalchemy import select

from app.connectors import registry
from app.connectors.watch import fetch as fetch_mod
from app.connectors.watch import noisy_target_ids
from app.connectors.watch.connector import WatchConnector
from app.core import pipeline
from app.models import Run, Source, WatchSnapshot, WatchTarget


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    monkeypatch.setattr(fetch_mod, "MIN_INTERVAL", 0.0)


def write_raw(tmp_path, targets):
    p = tmp_path / "raw.json"
    p.write_text(json.dumps({"targets": targets}))
    return p


def conn(sf=None):
    return WatchConnector(1, "custom-x", "X", sf)


def page_t(i, name, text, status="ok"):
    import hashlib
    return {"id": i, "kpi_name": name, "url": f"https://h/{i}", "kind": "page", "status": status,
            "content_type": "text/html", "text": text,
            "sha256": hashlib.sha256(text.encode()).hexdigest(), "error": None}


def err_t(i, name):
    return {"id": i, "kpi_name": name, "url": f"https://h/{i}", "kind": "page", "status": "error",
            "content_type": "", "error": "HTTP 500"}


def test_binary_content_hashed(tmp_path):
    t = {"id": 1, "kpi_name": "Doc", "url": "https://h/d.pdf", "kind": "page", "status": "ok",
         "content_type": "application/pdf", "binary": True, "text": "binary content changed",
         "sha256": "ab" * 32, "error": None}
    res = conn().parse(write_raw(tmp_path, [t]))
    rows = list(res.rows)
    assert len(rows) == 1 and rows[0].dataset == "page" and rows[0].value == "ab" * 32
    assert res.complete and [i.code for i in res.indicators] == ["Doc"]


def test_api_rows(tmp_path):
    t = {"id": 1, "kpi_name": "Api", "url": "u", "kind": "api", "status": "ok",
         "content_type": "application/json", "json": {"a": {"b": [{"c": 1}]}, "d": None}, "error": None}
    rows = list(conn().parse(write_raw(tmp_path, [t])).rows)
    assert {(r.dataset, r.value, r.dims["path"]) for r in rows} == {
        ("api", "1", "a.b[0].c"), ("api", "null", "d")}


def test_parse_partial_failure_incomplete(tmp_path):
    res = conn().parse(write_raw(tmp_path, [page_t(1, "Good", "hello"), err_t(2, "Bad")]))
    assert len(list(res.rows)) == 1
    assert res.complete is False
    assert any("Bad" in w for w in res.warnings)


def test_all_targets_failed_raises(tmp_path):
    with pytest.raises(RuntimeError, match="all targets failed"):
        conn().parse(write_raw(tmp_path, [err_t(1, "A"), err_t(2, "B")]))


@pytest.fixture
def wenv(db_session, session_factory):
    src = Source(slug="custom-x", name="X", area="custom", access="watch")
    db_session.add(src)
    db_session.flush()
    tgt = WatchTarget(source_id=src.id, kpi_name="Price", url="https://h.example/p", kind="page")
    db_session.add(tgt)
    db_session.flush()
    return src, tgt, session_factory


def serve(body):
    respx.get("https://h.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://h.example/p").mock(
        return_value=httpx.Response(200, text=f"<main><p>{body}</p></main>",
                                    headers={"content-type": "text/html"}))


@respx.mock
def test_pipeline_end_to_end_watch_source(wenv, db_session):
    src, tgt, sf = wenv
    serve("v1")
    assert pipeline.run_source(sf, "custom-x").status == "baseline"
    respx.clear()
    serve("v2")
    run = pipeline.run_source(sf, "custom-x")
    assert run.status == "ok" and run.altered == 1
    db_session.refresh(tgt)
    assert tgt.last_status == "ok" and tgt.last_checked_at is not None
    snaps = db_session.scalars(
        select(WatchSnapshot).where(WatchSnapshot.target_id == tgt.id).order_by(WatchSnapshot.id)).all()
    assert [s.changed for s in snaps] == [False, True]
    assert snaps[1].content == "v2"


def run_n(wenv, db_session, bodies):
    src, tgt, sf = wenv
    for b in bodies:
        with respx.mock:
            serve(b)
            pipeline.run_source(sf, "custom-x")
    return noisy_target_ids(db_session, src.id), tgt


def test_noisy_flag_after_9_of_10_changes(wenv, db_session):
    noisy, tgt = run_n(wenv, db_session, [f"v{i}" for i in range(10)])  # 9 changes
    assert noisy == {tgt.id}


def test_not_noisy_with_8_changes(wenv, db_session):
    bodies = ["v0", "v0"] + [f"v{i}" for i in range(1, 9)]  # 10 snapshots, 8 changes
    noisy, _ = run_n(wenv, db_session, bodies)
    assert noisy == set()


@respx.mock
def test_snapshot_retention_keeps_at_least_ten(wenv, db_session):
    src, tgt, sf = wenv
    for i in range(14):
        respx.clear()
        serve(f"v{i}")
        pipeline.run_source(sf, "custom-x")
    n = len(db_session.scalars(select(WatchSnapshot).where(WatchSnapshot.target_id == tgt.id)).all())
    assert n == 10


@respx.mock
def test_hook_failure_does_not_fail_run(wenv, db_session, monkeypatch):
    src, tgt, sf = wenv
    serve("v1")

    def boom(self, session, run):
        raise RuntimeError("hook")

    monkeypatch.setattr(WatchConnector, "after_run", boom)
    assert pipeline.run_source(sf, "custom-x").status == "baseline"


@respx.mock
def test_download_blocked_and_error_targets(wenv, db_session, tmp_path):
    src, tgt, sf = wenv
    db_session.add(WatchTarget(source_id=src.id, kpi_name="Priv", url="https://h.example/private/x"))
    db_session.flush()
    respx.get("https://h.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n"))
    respx.get("https://h.example/p").mock(
        return_value=httpx.Response(200, text="<main>ok</main>", headers={"content-type": "text/html"}))
    dest = tmp_path / "r.json"
    WatchConnector(src.id, "custom-x", "X", sf).download(httpx.Client(), dest)
    data = {t["kpi_name"]: t for t in json.loads(dest.read_text())["targets"]}
    assert data["Price"]["status"] == "ok" and data["Price"]["text"] == "ok"
    assert data["Priv"]["status"] == "blocked"
