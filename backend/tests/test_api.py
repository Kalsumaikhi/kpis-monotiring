from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api import deps
from app.api.main import create_app
from app.config import get_settings
from app.connectors import registry
from app.core import pipeline
from app.core.canonical import RawRow
from app.models import Change, DataPoint, Run, Source
from tests.conftest import FakeConnector, row, seed

AUTH = ("anyone", "secret")


@pytest.fixture
def client(db_session, session_factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "dashboard_password", "secret")
    app = create_app()
    app.dependency_overrides[deps.get_session] = lambda: db_session
    app.dependency_overrides[deps.get_session_factory] = lambda: session_factory
    c = TestClient(app)
    c.auth = AUTH
    return c


def test_requires_password(client):
    assert client.get("/api/sources", auth=None).status_code == 401
    assert client.get("/api/sources", auth=("u", "wrong")).status_code == 401
    assert client.get("/api/sources", auth=("u", "secret")).status_code == 200


def test_empty_password_config_returns_503(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "dashboard_password", "")
    for auth in (None, ("u", "")):
        r = client.get("/api/sources", auth=auth)
        assert r.status_code == 503
        assert r.json() == {"detail": "dashboard password not configured"}


def test_placeholder_password_returns_503(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "dashboard_password", "change-me")
    r = client.get("/api/sources", auth=("u", "change-me"))
    assert r.status_code == 503
    assert r.json() == {"detail": "dashboard password not configured"}


def test_health_is_public(client):
    r = client.get("/api/health", auth=None)
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_sources_list_shape(client, db_session, source):
    now = datetime.now(timezone.utc)
    seed(db_session, source, [row("AT", "2020", 1), row("BE", "2020", 2)])
    old = Run(source_id=source.id, status="ok", finished_at=now, added=1, altered=2, removed=3)
    db_session.add(old)
    db_session.flush()
    db_session.add_all([
        Run(source_id=source.id, status="failed"),
        Run(source_id=source.id, status="failed"),
    ])
    db_session.add_all([
        Change(run_id=old.id, source_id=source.id, key_hash="a" * 40, change_type="added",
               dataset="d", indicator="i", country="AT", period="2020"),
        Change(run_id=old.id, source_id=source.id, key_hash="b" * 40, change_type="added",
               dataset="d", indicator="i", country="BE", period="2020",
               created_at=now - timedelta(days=45)),
    ])
    db_session.commit()
    items = client.get("/api/sources").json()
    assert len(items) == 1
    s = items[0]
    assert set(s) == {"slug", "name", "area", "access", "enabled", "cron", "last_run",
                      "last_change_at", "rows_tracked", "changes_30d", "failing_streak",
                      "attribution", "incomplete", "noisy_targets", "last_success_at"}
    assert s["rows_tracked"] == 2
    assert s["changes_30d"] == 1
    assert s["failing_streak"] == 2
    assert s["last_change_at"] is not None
    assert set(s["last_run"]) == {"id", "status", "finished_at", "added", "altered", "removed"}
    assert s["last_run"]["status"] == "failed"


def test_sources_last_success_ignores_running_latest(client, db_session, source):
    t = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    db_session.add_all([
        Run(source_id=source.id, status="ok", finished_at=t),
        Run(source_id=source.id, status="failed", finished_at=t + timedelta(days=1)),
        Run(source_id=source.id, status="running"),
    ])
    db_session.commit()
    s = client.get("/api/sources").json()[0]
    assert s["last_run"]["status"] == "running"
    assert s["last_success_at"].startswith("2026-11-01T12:00:00")


def test_sources_last_success_orders_by_finished_at(client, db_session, source):
    newer = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    db_session.add(Run(source_id=source.id, status="ok", finished_at=newer))
    db_session.flush()
    db_session.add(Run(source_id=source.id, status="ok", finished_at=newer - timedelta(days=3)))
    db_session.commit()
    s = client.get("/api/sources").json()[0]
    assert s["last_success_at"].startswith("2026-11-05T12:00:00")


def test_sources_list_source_without_runs(client, source):
    s = client.get("/api/sources").json()[0]
    assert s["last_success_at"] is None
    assert s["last_run"] is None and s["last_change_at"] is None
    assert (s["rows_tracked"], s["changes_30d"], s["failing_streak"]) == (0, 0, 0)


def test_patch_source(client, db_session, source):
    r = client.patch(f"/api/sources/{source.slug}", json={"enabled": False, "cron": "*/5 * * * *"})
    assert r.status_code == 200
    db_session.refresh(source)
    assert source.enabled is False and source.cron == "*/5 * * * *"
    assert client.patch("/api/sources/nope", json={"enabled": True}).status_code == 404


def test_patch_invalid_cron_422(client, db_session, source):
    r = client.patch(f"/api/sources/{source.slug}", json={"cron": "not a cron"})
    assert r.status_code == 422
    db_session.refresh(source)
    assert source.cron == "0 3 * * *"


def test_run_now_creates_queued_and_409_on_double(client, db_session, source):
    r = client.post(f"/api/sources/{source.slug}/run")
    assert r.status_code == 202
    run = db_session.get(Run, r.json()["run_id"])
    assert run.status == "queued" and run.trigger == "manual"
    assert client.post(f"/api/sources/{source.slug}/run").status_code == 409
    run.status = "running"
    db_session.commit()
    assert client.post(f"/api/sources/{source.slug}/run").status_code == 409
    run.status = "ok"
    db_session.commit()
    assert client.post(f"/api/sources/{source.slug}/run").status_code == 202


def test_run_now_unknown_source_404(client):
    assert client.post("/api/sources/nope/run").status_code == 404


def test_runs_pagination_and_filter(client, db_session, source):
    db_session.add_all(
        [Run(source_id=source.id, status="failed" if n % 4 == 0 else "ok") for n in range(120)]
    )
    db_session.commit()
    r = client.get("/api/runs?page_size=50").json()
    assert len(r["items"]) == 50 and r["total"] == 120
    ids = [i["id"] for i in r["items"]]
    assert ids == sorted(ids, reverse=True)
    p3 = client.get("/api/runs?page_size=50&page=3").json()
    assert len(p3["items"]) == 20
    f = client.get("/api/runs?status=failed&page_size=200").json()
    assert f["total"] == 30 and all(i["status"] == "failed" for i in f["items"])
    assert client.get("/api/runs?source=nope").json()["total"] == 0
    assert client.get("/api/runs?page_size=201").status_code == 422


def _run_with_changes(db_session, source):
    run = Run(source_id=source.id, status="ok")
    db_session.add(run)
    db_session.flush()
    specs = [("AT", "added", None, Decimal("1.50")), ("BE", "altered", Decimal("2"), Decimal("3")),
             ("AT", "removed", Decimal("4"), None)]
    for n, (country, ct, old, new) in enumerate(specs):
        db_session.add(Change(run_id=run.id, source_id=source.id, key_hash=str(n) * 40,
                              change_type=ct, dataset="d", indicator="i", country=country,
                              period=str(2020 + n), old_num=old, new_num=new))
    db_session.commit()
    return run


def test_changes_filter_by_country(client, db_session, source):
    run = _run_with_changes(db_session, source)
    r = client.get(f"/api/runs/{run.id}/changes?country=AT").json()
    assert r["total"] == 2 and {i["country"] for i in r["items"]} == {"AT"}
    assert "pending" not in r
    r = client.get(f"/api/runs/{run.id}/changes?change_type=altered").json()
    assert r["items"][0]["old"] == "2" and r["items"][0]["new"] == "3"
    assert set(r["items"][0]) == {"change_type", "dataset", "indicator", "country", "period",
                                  "dims", "old", "new"}
    added = client.get(f"/api/runs/{run.id}/changes?change_type=added").json()["items"][0]
    assert added["old"] is None and added["new"] == "1.50"
    assert client.get(f"/api/runs/{run.id}/changes?change_type=bogus").status_code == 422
    assert client.get("/api/runs/999999/changes").status_code == 404


def test_run_detail_has_top_indicators(client, db_session, source):
    run = _run_with_changes(db_session, source)
    r = client.get(f"/api/runs/{run.id}").json()
    assert r["source"] == source.slug and r["status"] == "ok"
    assert r["top_indicators"] == [{"indicator": "i", "count": 3}]
    assert client.get("/api/runs/999999").status_code == 404


def test_approve_reject_state_errors(client, db_session, source):
    ok = Run(source_id=source.id, status="ok")
    db_session.add(ok)
    db_session.commit()
    assert client.post(f"/api/runs/{ok.id}/approve").status_code == 409
    assert client.post(f"/api/runs/{ok.id}/reject").status_code == 409
    assert client.post("/api/runs/999999/approve").status_code == 404


@pytest.fixture
def pending(db_session, session_factory, monkeypatch):
    """A suspect_pending run: baseline of 5 rows, then a fetch missing 2 and changing 1."""
    src = Source(slug="fake", name="Fake", area="test", access="manual")
    db_session.add(src)
    db_session.commit()  # the API rolls its session back; keep the source
    conn = FakeConnector()
    monkeypatch.setitem(registry.CONNECTORS, "fake", conn)
    raw = lambda c, v: RawRow("d", "i", c, "2020", v)  # noqa: E731
    conn.rows = [raw(c, 1) for c in ("AT", "BE", "CZ", "DE", "ES")]
    pipeline.run_source(session_factory, "fake")
    conn.rows = [raw("AT", 1), raw("BE", 9), raw("CZ", 1), raw("FR", 5)]
    run = pipeline.run_source(session_factory, "fake")
    assert run.status == "suspect_pending"
    return src, run


def test_pending_run_changes_endpoint_and_approve(client, db_session, session_factory, pending):
    src, run = pending
    r = client.get(f"/api/runs/{run.id}/changes?change_type=removed").json()
    assert r["pending"] is True and r["total"] == 2
    assert {i["country"] for i in r["items"]} == {"DE", "ES"}
    assert r["items"][0]["old"] == "1"
    a = client.post(f"/api/runs/{run.id}/approve")
    assert a.status_code == 202 and a.json()["status"] == "approve_queued"
    from app.worker import poll_queued_once
    poll_queued_once(session_factory, None)
    after = client.get(f"/api/runs/{run.id}/changes").json()
    assert "pending" not in after and after["total"] == 4


def test_reject_pending(client, db_session, pending):
    _, run = pending
    r = client.post(f"/api/runs/{run.id}/reject")
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert client.post(f"/api/runs/{run.id}/reject").status_code == 409


def test_reject_source_busy_409(client, pending, held_lock):
    src, run = pending
    held_lock(src.id)
    r = client.post(f"/api/runs/{run.id}/reject")
    assert r.status_code == 409 and r.json()["detail"] == "source busy"


def _api_routes():
    spec = create_app().openapi()["paths"]
    return [(m.upper(), p.replace("{run_id}", "1").replace("{slug}", "x"))
            for p, ops in spec.items() if p != "/api/health" for m in ops]


@pytest.mark.parametrize("method,path", _api_routes())
def test_every_api_route_requires_auth(client, method, path):
    assert client.request(method, path, auth=None).status_code == 401


def test_docs_not_exposed(client):
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(p, auth=None).status_code == 404


def test_second_queued_insert_is_409(client, db_session, source, monkeypatch):
    # lose the race: the pre-check sees nothing but the unique index rejects the insert
    db_session.add(Run(source_id=source.id, status="queued"))
    db_session.commit()
    monkeypatch.setattr("app.api.routes_sources._has_open_run", lambda *a: False)
    assert client.post(f"/api/sources/{source.slug}/run").status_code == 409
