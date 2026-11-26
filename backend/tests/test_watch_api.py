import io
from datetime import datetime, timedelta, timezone

import httpx
import openpyxl
import pytest
import respx
from sqlalchemy import func, select

from app import watch_import
from app.api import deps
from app.api.main import create_app
from app.config import get_settings
from app.connectors.watch import fetch as fetch_mod
from app.core import pipeline
from app.models import Change, DataPoint, ImportPreview, Indicator, Run, Source, WatchSnapshot, WatchTarget
from tests.conftest import api_client

AUTH = ("anyone", "secret")


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    monkeypatch.setattr(fetch_mod, "MIN_INTERVAL", 0.0)
    real = fetch_mod.socket.getaddrinfo

    def fake(host, *a, **k):
        if host in ("localhost", "127.0.0.1"):  # the test DB
            return real(host, *a, **k)
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(fetch_mod.socket, "getaddrinfo", fake)


@pytest.fixture
def client(db_session, session_factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "dashboard_password", "secret")
    app = create_app()
    app.dependency_overrides[deps.get_session] = lambda: db_session
    app.dependency_overrides[deps.get_session_factory] = lambda: session_factory
    c = api_client(app)
    c.auth = AUTH
    return c


def create(client, source="Acme", kpi="Price", url="https://h.example/p", **extra):
    return client.post("/api/custom-sources",
                       json={"source_name": source, "kpi_name": kpi, "url": url, **extra})


def xlsx(rows, header=("source  NAME", "kpi name", "Link ")):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(header))
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


XLSX = ("sheet.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def upload(client, name, data, ctype="application/octet-stream"):
    return client.post("/api/watch-targets/import/preview", files={"file": (name, data, ctype)})


def serve(pages):
    respx.get("https://h.example/robots.txt").mock(return_value=httpx.Response(404))
    for path, body in pages.items():
        respx.get(f"https://h.example/{path}").mock(
            return_value=httpx.Response(200, text=f"<main><p>{body}</p></main>",
                                        headers={"content-type": "text/html"}))


# --- CRUD ---------------------------------------------------------------------

def test_create_target_creates_source_and_slug(client, db_session):
    r = create(client)
    assert r.status_code == 201
    body = r.json()
    assert body["kpi_name"] == "Price" and body["url"] == "https://h.example/p"
    src = db_session.scalars(select(Source).where(Source.name == "Acme")).one()
    assert src.slug == "custom-acme" and src.area == "custom" and src.access == "watch"
    # second KPI joins the same source; a differently named source with a clashing slug gets a suffix
    assert create(client, kpi="Other", url="https://h.example/o").status_code == 201
    assert db_session.scalars(select(Source).where(Source.name == "Acme")).all() == [src]
    assert create(client, source="Acme!", kpi="X", url="https://h.example/x").status_code == 201
    slugs = sorted(s.slug for s in db_session.scalars(select(Source).where(Source.area == "custom")))
    assert slugs == ["custom-acme", "custom-acme-2"]
    listing = client.get("/api/custom-sources").json()
    acme = next(s for s in listing if s["slug"] == "custom-acme")
    assert {"slug", "name", "enabled", "cron", "targets"} <= set(acme)
    assert len(acme["targets"]) == 2 and "noisy" in acme["targets"][0]


def test_create_target_rejects_bad_url_and_duplicate(client):
    assert create(client, url="ftp://x/y").status_code == 422
    assert create(client, url="not a url").status_code == 422
    assert create(client, source="  ").status_code == 422
    assert create(client, kpi="").status_code == 422
    assert create(client).status_code == 201
    assert create(client, kpi="Another").status_code == 409  # same source + url
    assert create(client, url="https://h.example/q").status_code == 409  # same kpi name


def test_requires_auth(client):
    assert client.get("/api/custom-sources", auth=None).status_code == 401


def test_delete_target_no_removed_changes(client, db_session, session_factory):
    tid = create(client).json()["id"]
    create(client, kpi="Two", url="https://h.example/two")
    with respx.mock:
        serve({"p": "v1", "two": "x"})
        pipeline.run_source(session_factory, "custom-acme")
    assert db_session.scalars(select(DataPoint)).all()
    assert client.delete(f"/api/watch-targets/{tid}").status_code == 204
    pts = db_session.scalars(select(DataPoint.indicator)).all()
    assert pts == ["Two"]
    assert db_session.scalars(select(Change)).all() == []
    assert client.delete(f"/api/watch-targets/{tid}").status_code == 404


def _seed_source_rows(db_session, slug):
    """Rows in every table that points at the source, plus a raw file on disk."""
    from pathlib import Path
    from app.models import ChangeRollup, PendingChange, Upload, WatchSnapshot
    src = db_session.scalar(select(Source).where(Source.slug == slug))
    run = Run(source_id=src.id, status="ok")
    db_session.add(run)
    db_session.flush()
    raw = Path(get_settings().raw_dir) / slug / f"{run.id}.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("{}")
    run.raw_path = str(raw)
    tid = db_session.scalar(select(WatchTarget.id).where(WatchTarget.source_id == src.id))
    db_session.add_all([
        Change(run_id=run.id, source_id=src.id, key_hash="a" * 40, change_type="altered",
               dataset="page", indicator="Price", country="", period="p"),
        PendingChange(run_id=run.id, source_id=src.id, key_hash="b" * 40, change_type="added",
                      dataset="page", indicator="Price", country="", period="p"),
        DataPoint(source_id=src.id, key_hash="c" * 40, dataset="page", indicator="Price", value_hash="e" * 40,
                  country="", period="p"),
        Indicator(source_id=src.id, code="Price", name="Price"),
        ChangeRollup(source_id=src.id, indicator="Price", day=datetime.now().date(), altered=1),
        Upload(source_id=src.id, who="x", filename="f", run_id=run.id),
        WatchSnapshot(target_id=tid, run_id=run.id, content="x", content_hash="d" * 64,
                      changed=False),
    ])
    db_session.commit()
    return src, raw


def _counts(db_session, source_id):
    from app.models import ChangeRollup, PendingChange, Upload, WatchSnapshot
    out = {}
    for m in (Run, Change, PendingChange, DataPoint, Indicator, ChangeRollup, Upload, WatchTarget):
        out[m.__tablename__] = db_session.scalar(
            select(func.count()).select_from(m).where(m.source_id == source_id))
    return out


def test_delete_custom_source_removes_everything(client, db_session):
    create(client)
    create(client, source="Other", url="https://h.example/o")
    acme, raw = _seed_source_rows(db_session, "custom-acme")
    other, other_raw = _seed_source_rows(db_session, "custom-other")
    before_other = _counts(db_session, other.id)
    assert all(before_other.values()) and raw.exists()
    assert client.delete("/api/custom-sources/custom-acme").status_code == 204
    assert db_session.scalar(select(Source).where(Source.slug == "custom-acme")) is None
    assert not raw.exists()
    assert all(v == 0 for v in _counts(db_session, acme.id).values())
    assert db_session.scalar(select(func.count()).select_from(WatchSnapshot)) == 1
    assert _counts(db_session, other.id) == before_other
    assert other_raw.exists()
    assert client.delete("/api/custom-sources/custom-acme").status_code == 404


def test_delete_custom_source_never_removes_outside_raw_dir(db_session, session_factory):
    from pathlib import Path
    from app.api.routes_watch import delete_custom_source
    outside = Path(get_settings().raw_dir).parent / "outside-keep"
    outside.mkdir(exist_ok=True)
    (outside / "f.txt").write_text("x")
    for slug in ("../outside-keep", "x/../../outside-keep", ".."):
        db_session.add(Source(slug=slug, name=slug, area="custom", access="watch"))
        db_session.commit()
        delete_custom_source(slug, db_session)
        assert db_session.scalar(select(Source).where(Source.slug == slug)) is None
        assert (outside / "f.txt").exists()


def test_delete_custom_source_with_queued_run_is_safe_for_worker(client, db_session, session_factory):
    from app.worker import poll_queued_once
    create(client)
    sid = db_session.scalar(select(Source.id).where(Source.slug == "custom-acme"))
    db_session.add(Run(source_id=sid, status="queued", trigger="manual"))
    db_session.commit()
    assert client.delete("/api/custom-sources/custom-acme").status_code == 204
    assert db_session.scalars(select(Run)).all() == []
    assert poll_queued_once(session_factory, None) == 0


def test_delete_custom_source_refuses_builtin(client, db_session):
    db_session.add(Source(slug="wdi", name="WDI", area="economic", access="api"))
    db_session.commit()
    assert client.delete("/api/custom-sources/wdi").status_code == 404
    assert client.delete("/api/custom-sources/nope").status_code == 404
    assert db_session.scalar(select(Source).where(Source.slug == "wdi")) is not None


def test_delete_custom_source_409_while_locked(client, db_session, held_lock):
    create(client)
    sid = db_session.scalar(select(Source.id).where(Source.slug == "custom-acme"))
    held_lock(sid)
    r = client.delete("/api/custom-sources/custom-acme")
    assert r.status_code == 409
    assert r.json()["detail"] == "a check is running for this source; try again when it finishes"
    assert db_session.scalar(select(Source).where(Source.slug == "custom-acme")) is not None


def test_delete_custom_source_silent(client, db_session):
    create(client)
    _seed_source_rows(db_session, "custom-acme")
    with respx.mock(assert_all_called=False) as m:
        assert client.delete("/api/custom-sources/custom-acme").status_code == 204
        assert len(m.calls) == 0


def test_delete_last_target_keeps_source(client, db_session):
    tid = create(client).json()["id"]
    assert client.delete(f"/api/watch-targets/{tid}").status_code == 204
    assert db_session.scalars(select(Source).where(Source.slug == "custom-acme")).one()
    s = next(s for s in client.get("/api/custom-sources").json() if s["slug"] == "custom-acme")
    assert s["targets"] == []


def test_patch_validation_and_conflicts(client):
    a = create(client).json()["id"]
    create(client, kpi="B", url="https://h.example/b")
    assert client.patch(f"/api/watch-targets/{a}", json={"kpi_name": "B"}).status_code == 409
    assert client.patch(f"/api/watch-targets/{a}", json={"url": "https://h.example/b"}).status_code == 409
    assert client.patch(f"/api/watch-targets/{a}", json={"url": "ftp://x"}).status_code == 422
    assert client.patch(f"/api/watch-targets/{a}", json={"kind": "bogus"}).status_code == 422
    assert client.patch("/api/watch-targets/99999", json={"enabled": False}).status_code == 404
    r = client.patch(f"/api/watch-targets/{a}", json={"selector": "main", "use_browser": True})
    assert r.status_code == 200 and r.json()["selector"] == "main" and r.json()["use_browser"] is True


# --- test link ------------------------------------------------------------------

@respx.mock
def test_test_link_page_and_json(client):
    respx.get("https://h.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://h.example/page").mock(return_value=httpx.Response(
        200, text="<main><p>" + "word " * 1000 + "</p></main>", headers={"content-type": "text/html"}))
    respx.get("https://h.example/data").mock(return_value=httpx.Response(
        200, json={"a": {"b": 1}, "c": [1, 2]}, headers={"content-type": "application/json"}))
    r = client.post("/api/watch-targets/test", json={"url": "https://h.example/page"}).json()
    assert r["status"] == "ok" and r["kind"] == "page" and 0 < len(r["text_preview"]) <= 2000
    r = client.post("/api/watch-targets/test", json={"url": "https://h.example/data"}).json()
    assert r["status"] == "ok" and r["kind"] == "api"
    assert {"path": "a.b", "value": "1"} in r["json_preview"]
    bad = client.post("/api/watch-targets/test", json={"url": "ftp://x"})
    assert bad.status_code == 422


@respx.mock
def test_test_link_reports_errors_and_writes_nothing(client, db_session):
    respx.get("https://h.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://h.example/x").mock(return_value=httpx.Response(500))
    r = client.post("/api/watch-targets/test", json={"url": "https://h.example/x"}).json()
    assert r["status"] == "error" and r["error"]
    assert db_session.scalars(select(Source)).all() == []


# --- import ---------------------------------------------------------------------

def test_import_preview_messy_sheet(client, db_session):
    data = xlsx([
        ("Acme", "Price", "https://h.example/p"),
        (None, None, None),
        ("Acme", "Price", "https://h.example/p"),
        ("Acme", "Bad", "ftp://x"),
    ])
    r = upload(client, "k.xlsx", data)
    assert r.status_code == 200
    body = r.json()
    assert [x["status"] for x in body["rows"]] == ["new_source", "duplicate", "invalid"]
    assert [x["row"] for x in body["rows"]] == [2, 4, 5]
    assert body["rows"][2]["error"]
    assert db_session.get(ImportPreview, body["token"]) is not None


def test_import_existing_source_and_existing_duplicate(client):
    create(client)
    data = xlsx([("Acme", "New", "https://h.example/n"), ("Acme", "Dup", "https://h.example/p"),
                 ("Acme", "Price", "https://h.example/z")])
    st = [x["status"] for x in upload(client, "k.xlsx", data).json()["rows"]]
    assert st == ["existing_source", "duplicate", "duplicate"]  # last: kpi name taken


def test_import_missing_column_422(client):
    data = xlsx([("a", "b")], header=("Source name", "KPI name"))
    r = upload(client, "k.xlsx", data)
    assert r.status_code == 422 and r.json()["detail"] == "missing column: Link"


def test_import_rejects_unknown_type_and_big_file(client):
    assert upload(client, "k.txt", b"x").status_code == 422
    r = upload(client, "k.csv", b"a" * (2 * 1024 * 1024 + 1))
    assert r.status_code == 413


def test_import_confirm_creates_and_token_single_use(client, db_session):
    create(client)
    data = xlsx([("Acme", "New", "https://h.example/n"), ("Fresh", "F1", "https://h.example/f1"),
                 ("Fresh", "F2", "https://h.example/f2"), ("Acme", "Price", "https://h.example/p"),
                 ("Acme", "Bad", "ftp://x")])
    tok = upload(client, "k.xlsx", data).json()["token"]
    r = client.post("/api/watch-targets/import/confirm", json={"token": tok})
    assert r.status_code == 200
    assert r.json() == {"created_sources": 1, "created_targets": 3, "skipped": 2, "skipped_rows": []}
    assert db_session.scalars(select(Source).where(Source.slug == "custom-fresh")).one()
    assert client.post("/api/watch-targets/import/confirm", json={"token": tok}).status_code == 410
    assert client.post("/api/watch-targets/import/confirm", json={"token": "nope"}).status_code == 410


def test_import_confirm_expired_token(client, db_session):
    data = xlsx([("Acme", "Price", "https://h.example/p")])
    tok = upload(client, "k.xlsx", data).json()["token"]
    pv = db_session.get(ImportPreview, tok)
    pv.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db_session.flush()
    assert client.post("/api/watch-targets/import/confirm", json={"token": tok}).status_code == 410


def test_import_csv(client):
    csv = ("﻿Source Name;KPI  name;LINK\r\n"
           "Acme;Price;https://h.example/p\r\n;;\r\nAcme;Stock;https://h.example/s\r\n").encode("utf-8")
    r = upload(client, "k.csv", csv)
    assert r.status_code == 200
    assert [x["kpi_name"] for x in r.json()["rows"]] == ["Price", "Stock"]
    assert [x["row"] for x in r.json()["rows"]] == [2, 4]
    comma = b"source name,kpi name,link\nA,K,https://x.example/\n"
    assert upload(client, "k.csv", comma).json()["rows"][0]["status"] == "new_source"


def test_parse_sheet_unit():
    rows = watch_import.parse_sheet("a.csv", b"Source name,KPI name,Link\nA,K,https://x/\n")
    assert rows == [{"row": 2, "source_name": "A", "kpi_name": "K", "url": "https://x/"}]


# --- R9: baselines --------------------------------------------------------------

@pytest.fixture
def history(client, db_session, session_factory):
    """Source 'Acme' with one KPI that already has a baseline run."""
    create(client)
    with respx.mock:
        serve({"p": "v1"})
        assert pipeline.run_source(session_factory, "custom-acme").status == "baseline"
    return client


def run(session_factory, pages):
    with respx.mock:
        serve(pages)
        return pipeline.run_source(session_factory, "custom-acme")


def test_new_kpis_do_not_alert_on_first_fetch(history, db_session, session_factory):
    data = xlsx([("Acme", f"K{i}", f"https://h.example/k{i}") for i in range(3)])
    tok = upload(history, "k.xlsx", data).json()["token"]
    history.post("/api/watch-targets/import/confirm", json={"token": tok})
    pages = {"p": "v1", "k0": "a", "k1": "b", "k2": "c"}
    r1 = run(session_factory, pages)
    assert r1.status == "no_change" and (r1.added, r1.altered, r1.removed) == (0, 0, 0)
    assert db_session.scalars(select(Change)).all() == []
    assert {d.indicator for d in db_session.scalars(select(DataPoint))} == {"Price", "K0", "K1", "K2"}
    pages["k1"] = "changed"
    r2 = run(session_factory, pages)
    assert r2.status == "ok" and r2.altered == 1 and r2.added == 0
    t = db_session.scalars(select(WatchTarget).where(WatchTarget.kpi_name == "K0")).one()
    assert t.needs_baseline is False


def test_disable_deletes_points_silently_and_reenable_rebaselines(history, db_session, session_factory):
    tid = db_session.scalars(select(WatchTarget.id)).one()
    create(history, kpi="Keep", url="https://h.example/keep")
    run(session_factory, {"p": "v1", "keep": "k"})
    assert history.patch(f"/api/watch-targets/{tid}", json={"enabled": False}).status_code == 200
    assert {d.indicator for d in db_session.scalars(select(DataPoint))} == {"Keep"}
    r = run(session_factory, {"p": "v1", "keep": "k"})
    assert r.removed == 0 and db_session.scalars(select(Change)).all() == []
    assert history.patch(f"/api/watch-targets/{tid}", json={"enabled": True}).status_code == 200
    r = run(session_factory, {"p": "v9", "keep": "k"})
    assert r.status == "no_change" and db_session.scalars(select(Change)).all() == []


def test_rename_is_silent(history, db_session, session_factory):
    tid = db_session.scalars(select(WatchTarget.id)).one()
    assert history.patch(f"/api/watch-targets/{tid}", json={"kpi_name": "Cost"}).status_code == 200
    r = run(session_factory, {"p": "v1"})
    assert r.status == "no_change"
    assert db_session.scalars(select(Change)).all() == []
    assert [d.indicator for d in db_session.scalars(select(DataPoint))] == ["Cost"]
    ind = db_session.scalars(select(Indicator).where(Indicator.source_id == db_session.scalars(
        select(Source.id).where(Source.slug == "custom-acme")).one())).all()
    assert [(i.code, i.name) for i in ind] == [("Cost", "Cost")]


def test_url_change_is_new_baseline(history, db_session, session_factory):
    tid = db_session.scalars(select(WatchTarget.id)).one()
    assert history.patch(f"/api/watch-targets/{tid}", json={"url": "https://h.example/q"}).status_code == 200
    assert db_session.scalars(select(DataPoint)).all() == []
    r = run(session_factory, {"q": "other"})
    assert db_session.scalars(select(Change)).all() == []
    assert r.status in ("no_change", "baseline") and (r.added, r.altered, r.removed) == (0, 0, 0)
    assert [d.indicator for d in db_session.scalars(select(DataPoint))] == ["Price"]


# --- review fixes ---------------------------------------------------------------

def test_mutations_409_while_source_running(client, db_session, held_lock):
    tid = create(client).json()["id"]
    sid = db_session.scalars(select(Source.id)).one()
    held_lock(sid)
    for resp in (client.patch(f"/api/watch-targets/{tid}", json={"kpi_name": "New"}),
                 client.patch(f"/api/watch-targets/{tid}", json={"enabled": False}),
                 client.patch(f"/api/watch-targets/{tid}", json={"url": "https://h.example/z"}),
                 client.delete(f"/api/watch-targets/{tid}")):
        assert resp.status_code == 409 and resp.json()["detail"] == "source is running, try again"
    t = db_session.get(WatchTarget, tid)
    db_session.refresh(t)
    assert (t.kpi_name, t.url, t.enabled) == ("Price", "https://h.example/p", True)
    # harmless edits are not blocked
    assert client.patch(f"/api/watch-targets/{tid}", json={"selector": "main"}).status_code == 200


def test_rename_into_deleted_kpi_name(client, db_session, session_factory):
    a = create(client, kpi="A", url="https://h.example/a").json()["id"]
    b = create(client, kpi="B", url="https://h.example/b").json()["id"]
    with respx.mock:
        serve({"a": "1", "b": "2"})
        pipeline.run_source(session_factory, "custom-acme")
    assert client.delete(f"/api/watch-targets/{a}").status_code == 204
    sid = db_session.scalars(select(Source.id)).one()
    assert [i.code for i in db_session.scalars(select(Indicator).where(Indicator.source_id == sid))] == ["B"]
    # a stale indicator left by older code must not break the rename either
    db_session.add(Indicator(source_id=sid, code="A", name="A"))
    db_session.flush()
    assert client.patch(f"/api/watch-targets/{b}", json={"kpi_name": "A"}).status_code == 200
    assert [i.code for i in db_session.scalars(select(Indicator).where(Indicator.source_id == sid))] == ["A"]


def test_conflict_is_case_insensitive(client):
    create(client)
    assert create(client, kpi="PRICE", url="https://h.example/other").status_code == 409


def test_held_run_keeps_needs_baseline_and_approve_clears(client, db_session, session_factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "suspect_removal_ratio", 0.0)
    create(client, kpi="Old", url="https://h.example/old")
    with respx.mock:
        serve({"old": "v1"})
        assert pipeline.run_source(session_factory, "custom-acme").status == "baseline"
    new = create(client, kpi="New", url="https://h.example/new").json()["id"]
    gone = db_session.scalars(select(WatchTarget.id).where(WatchTarget.kpi_name == "Old")).one()
    # Old disappears from the fetch (disabled in DB is silent, so force a removal via a changed page
    # plus force_hold instead)
    with respx.mock:
        serve({"old": "v2", "new": "n"})
        held = pipeline.run_source(session_factory, "custom-acme", force_hold=True)
    assert held.status == "suspect_pending"
    t = db_session.get(WatchTarget, new)
    db_session.refresh(t)
    assert t.needs_baseline is True
    pipeline.approve_run(session_factory, held.id)
    db_session.refresh(t)
    assert t.needs_baseline is False
    assert {c.indicator for c in db_session.scalars(select(Change))} == {"Old"}
    assert gone  # silence unused


@pytest.mark.parametrize("mutation", ["rename", "url", "disable", "delete"])
def test_kpi_mutation_supersedes_held_runs(client, db_session, session_factory, mutation):
    from app.models import PendingChange

    a = create(client, kpi="A", url="https://h.example/a").json()["id"]
    create(client, kpi="B", url="https://h.example/b")
    with respx.mock:
        serve({"a": "1", "b": "2"})
        assert pipeline.run_source(session_factory, "custom-acme").status == "baseline"
        serve({"a": "1x", "b": "2x"})
        held = pipeline.run_source(session_factory, "custom-acme", force_hold=True)
    assert held.status == "suspect_pending"
    queued = Run(source_id=held.source_id, status="approve_queued")
    db_session.add(queued)
    db_session.flush()
    assert db_session.scalars(select(PendingChange).where(PendingChange.run_id == held.id)).all()
    resp = {
        "rename": lambda: client.patch(f"/api/watch-targets/{a}", json={"kpi_name": "A2"}),
        "url": lambda: client.patch(f"/api/watch-targets/{a}", json={"url": "https://h.example/a2"}),
        "disable": lambda: client.patch(f"/api/watch-targets/{a}", json={"enabled": False}),
        "delete": lambda: client.delete(f"/api/watch-targets/{a}"),
    }[mutation]()
    assert resp.status_code in (200, 204)
    held = db_session.get(Run, held.id)
    for r, msg in ((held, "superseded by a change to the source's KPIs"),
                   (queued, "superseded by a change to the source's KPIs before the approval was applied")):
        db_session.refresh(r)
        assert (r.status, r.error) == ("superseded", msg)
    assert db_session.scalars(select(PendingChange).where(PendingChange.run_id == held.id)).all() == []


def test_harmless_edit_keeps_held_runs(client, db_session, session_factory):
    a = create(client, kpi="A", url="https://h.example/a").json()["id"]
    with respx.mock:
        serve({"a": "1"})
        pipeline.run_source(session_factory, "custom-acme")
        serve({"a": "1x"})
        held = pipeline.run_source(session_factory, "custom-acme", force_hold=True)
    assert client.patch(f"/api/watch-targets/{a}", json={"selector": "main"}).status_code == 200
    held = db_session.get(Run, held.id)
    db_session.refresh(held)
    assert held.status == "suspect_pending"


# --- import parsing: headers, row limit, hyperlinks ------------------------------

def test_header_match_ignores_all_spaces_and_case():
    data = b"SourceName;KPIname; L i n k \nA;K;https://x/\n"
    assert watch_import.parse_sheet("a.csv", data) == [
        {"row": 2, "source_name": "A", "kpi_name": "K", "url": "https://x/"}]
    rows = watch_import.parse_sheet("a.xlsx", xlsx([("A", "K", "https://x/")],
                                                   header=("SOURCENAME", "Kpi Name", "link")))
    assert rows[0]["url"] == "https://x/"


@pytest.mark.parametrize("ext", ["csv", "xlsx"])
def test_import_stops_reading_after_max_rows(monkeypatch, ext):
    monkeypatch.setattr(watch_import, "MAX_ROWS", 3)
    body = [("A", f"K{i}", f"https://x/{i}") for i in range(50)]
    if ext == "csv":
        data = ("Source name,KPI name,Link\n" + "".join(f"{a},{k},{u}\n" for a, k, u in body)).encode()
        assert len(watch_import._csv_rows(data)) == 5  # header + MAX_ROWS + 1, not 51
    else:
        data = xlsx(body)
        assert len(watch_import._xlsx_rows(data)) == 5
    with pytest.raises(watch_import.SheetError, match="too many rows"):
        watch_import.parse_sheet(f"a.{ext}", data)
    # exactly MAX_ROWS is fine
    if ext == "csv":
        ok = b"Source name,KPI name,Link\n" + b"".join(f"A,{k},{u}\n".encode() for _, k, u in body[:3])
    else:
        ok = xlsx(body[:3])
    assert len(watch_import.parse_sheet(f"a.{ext}", ok)) == 3


def test_xlsx_hyperlink_cells_use_the_link_target():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Source name", "KPI name", "Link"])
    ws.append(["A", "Linked", "Click here"])
    ws["C2"].hyperlink = "https://h.example/target"
    ws.append(["A", "Formula", '=HYPERLINK("https://h.example/f?q=""x""","Go")'])
    ws.append(["A", "Plain", "https://h.example/plain"])
    ws.append(["A", "Lower", '=hyperlink( "https://h.example/low" )'])
    buf = io.BytesIO()
    wb.save(buf)
    rows = watch_import.parse_sheet("s.xlsx", buf.getvalue())
    assert [(r["kpi_name"], r["url"]) for r in rows] == [
        ("Linked", "https://h.example/target"),
        ("Formula", 'https://h.example/f?q="x"'),
        ("Plain", "https://h.example/plain"),
        ("Lower", "https://h.example/low"),
    ]


# --- unique KPI names per source (plan 3a task 5, item 1) -------------------------

NAME_TAKEN = "a KPI with this name already exists in this source"


def test_duplicate_kpi_name_409_message(client):
    a = create(client).json()["id"]
    r = create(client, kpi="price", url="https://h.example/q")
    assert r.status_code == 409 and r.json()["detail"] == NAME_TAKEN
    create(client, kpi="B", url="https://h.example/b")
    r = client.patch(f"/api/watch-targets/{a}", json={"kpi_name": "b"})
    assert r.status_code == 409 and r.json()["detail"] == NAME_TAKEN


def test_duplicate_kpi_name_race_hits_the_index_409(client, monkeypatch):
    """The app-level check can lose a race; the unique index then answers 409, never 500."""
    from app.api import routes_watch

    a = create(client).json()["id"]
    create(client, kpi="B", url="https://h.example/b")
    monkeypatch.setattr(routes_watch, "_conflict", lambda *a, **k: None)
    r = create(client, kpi="PRICE", url="https://h.example/q")
    assert r.status_code == 409 and r.json()["detail"] == NAME_TAKEN
    r = client.patch(f"/api/watch-targets/{a}", json={"kpi_name": "b"})
    assert r.status_code == 409 and r.json()["detail"] == NAME_TAKEN
    tok = upload(client, "k.xlsx", xlsx([("Acme", "Fresh", "https://h.example/f")])).json()["token"]
    create(client, kpi="fresh", url="https://h.example/f2")  # created since the preview
    r = client.post("/api/watch-targets/import/confirm", json={"token": tok})
    assert r.status_code == 409 and r.json()["detail"] == NAME_TAKEN


def test_import_preview_flags_duplicate_names_in_sheet(client):
    data = xlsx([("Acme", "Price", "https://h.example/a"), ("Acme", "PRICE", "https://h.example/b"),
                 ("Other", "price", "https://h.example/c")])
    st = [x["status"] for x in upload(client, "k.xlsx", data).json()["rows"]]
    assert st == ["new_source", "duplicate", "new_source"]


# --- target PATCH/DELETE under the source lock (plan 3a task 5, item 2) -----------

@pytest.mark.parametrize("call", ["rename", "disable", "delete"])
def test_target_deleted_while_waiting_for_lock_is_404(client, db_session, monkeypatch, call):
    """The target is gone by the time the lock is taken: 404, not 500."""
    from contextlib import contextmanager

    from sqlalchemy import delete as sa_delete

    from app.api import routes_watch

    tid = create(client).json()["id"]
    real = routes_watch._source_lock

    @contextmanager
    def lock_then_vanish(session, source_id, *a, **k):
        with real(session, source_id, *a, **k):
            session.execute(sa_delete(WatchTarget).where(WatchTarget.id == tid)
                            .execution_options(synchronize_session=False))  # as another request would
            yield

    monkeypatch.setattr(routes_watch, "_source_lock", lock_then_vanish)
    if call == "delete":
        r = client.delete(f"/api/watch-targets/{tid}")
    else:
        body = {"kpi_name": "New"} if call == "rename" else {"enabled": False}
        r = client.patch(f"/api/watch-targets/{tid}", json=body)
    assert r.status_code == 404 and r.json()["detail"] == "target not found"


def test_patch_recomputes_from_the_row_read_under_the_lock(client, db_session, monkeypatch):
    """A rename that lands while a disable waits for the lock is kept, not reverted."""
    from contextlib import contextmanager

    from sqlalchemy import update as sa_update

    from app.api import routes_watch

    tid = create(client).json()["id"]
    real = routes_watch._source_lock

    @contextmanager
    def lock_after_rename(session, source_id, *a, **k):
        with real(session, source_id, *a, **k):
            session.execute(sa_update(WatchTarget).where(WatchTarget.id == tid)
                            .values(kpi_name="Renamed")
                            .execution_options(synchronize_session=False))
            yield

    monkeypatch.setattr(routes_watch, "_source_lock", lock_after_rename)
    r = client.patch(f"/api/watch-targets/{tid}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["kpi_name"] == "Renamed" and r.json()["enabled"] is False


def test_import_confirm_says_why_a_row_was_skipped(client):
    """Rows that became duplicates after the preview are skipped with a reason, per row."""
    create(client)  # Acme / Price / https://h.example/p
    data = xlsx([("Acme", "Fresh", "https://h.example/f"), ("Acme", "Linky", "https://h.example/l"),
                 ("Acme", "Fine", "https://h.example/fine"), ("Acme", "Price", "https://h.example/z")])
    tok = upload(client, "k.xlsx", data).json()["token"]
    create(client, kpi="FRESH", url="https://h.example/f2")  # name taken since the preview
    create(client, kpi="Other", url="https://h.example/l")  # link taken since the preview
    r = client.post("/api/watch-targets/import/confirm", json={"token": tok})
    assert r.status_code == 200
    out = r.json()
    assert (out["created_targets"], out["skipped"]) == (1, 3)
    assert out["skipped_rows"] == [
        {"row": 2, "source_name": "Acme", "kpi_name": "Fresh", "reason": NAME_TAKEN},
        {"row": 3, "source_name": "Acme", "kpi_name": "Linky",
         "reason": "a KPI with this link already exists"},
    ]  # row 5 was already a duplicate in the preview, which said so
