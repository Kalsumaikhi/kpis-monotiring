import pytest
from sqlalchemy import func, select

from app.api import deps
from app.config import get_settings
from app.connectors import registry
from app.core import pipeline
from app.core.canonical import RawRow
from app.api.main import create_app
from app.models import Change, DataPoint, PendingChange, Run, Source
from app.worker import poll_queued_once, sweep_stale_runs
from tests.conftest import FakeConnector, api_client

AUTH = ("u", "secret")


def raw(country, period, value):
    return RawRow("d", "i", country, period, value)


@pytest.fixture
def env(db_session, session_factory, monkeypatch):
    src = Source(slug="fake", name="Fake", area="test", access="manual")
    db_session.add(src)
    db_session.commit()
    conn = FakeConnector()
    monkeypatch.setitem(registry.CONNECTORS, "fake", conn)
    return src, conn, session_factory


@pytest.fixture
def client(db_session, session_factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "dashboard_password", "secret")
    app = create_app()
    app.dependency_overrides[deps.get_session] = lambda: db_session
    app.dependency_overrides[deps.get_session_factory] = lambda: session_factory
    c = api_client(app)
    c.auth = AUTH
    return c


def _held(env):
    src, conn, sf = env
    conn.rows = [raw(f"C{i}", "2020", i) for i in range(10)]
    pipeline.run_source(sf, "fake")
    conn.rows = conn.rows[:7]
    run = pipeline.run_source(sf, "fake")
    assert run.status == "suspect_pending"
    return run


def _n(db_session, model, **where):
    q = select(func.count()).select_from(model)
    for k, v in where.items():
        q = q.where(getattr(model, k) == v)
    return db_session.scalar(q)


def test_held_run_writes_pending_changes(env, db_session):
    src, _, _ = env
    run = _held(env)
    db_session.expire_all()
    rows = db_session.scalars(select(PendingChange).where(PendingChange.run_id == run.id)).all()
    assert len(rows) == 3 and {r.change_type for r in rows} == {"removed"}
    assert _n(db_session, DataPoint, source_id=src.id) == 10
    assert _n(db_session, Change, run_id=run.id) == 0


def test_force_hold_holds_small_change(env, db_session):
    src, conn, sf = env
    conn.rows = [raw(f"C{i}", "2020", i) for i in range(10)]
    pipeline.run_source(sf, "fake")
    conn.rows[0] = raw("C0", "2020", 99)
    run = pipeline.run_source(sf, "fake", force_hold=True)
    assert run.status == "suspect_pending"
    assert _n(db_session, PendingChange, run_id=run.id) == 1
    assert _n(db_session, Change, run_id=run.id) == 0


def test_force_hold_without_changes_is_no_change(env, db_session):
    _, conn, sf = env
    conn.rows = [raw("AT", "2020", 1)]
    pipeline.run_source(sf, "fake")
    assert pipeline.run_source(sf, "fake", force_hold=True).status == "no_change"


def test_force_hold_never_holds_baseline(env, db_session):
    _, conn, sf = env
    conn.rows = [raw("AT", "2020", 1)]
    run = pipeline.run_source(sf, "fake", force_hold=True)
    assert run.status == "baseline"
    assert _n(db_session, PendingChange) == 0


def test_approve_is_queued_and_double_approve_409(client, env, db_session):
    run = _held(env)
    r = client.post(f"/api/runs/{run.id}/approve")
    assert r.status_code == 202
    assert r.json() == {"run_id": run.id, "status": "approve_queued"}
    assert client.post(f"/api/runs/{run.id}/approve").status_code == 409
    assert client.post(f"/api/runs/{run.id}/reject").status_code == 409
    assert client.post("/api/runs/999999/approve").status_code == 404


def test_worker_applies_approve_queued(client, env, db_session):
    src, _, sf = env
    run = _held(env)
    assert client.post(f"/api/runs/{run.id}/approve").status_code == 202
    assert poll_queued_once(sf, None) == 1
    db_session.expire_all()
    done = db_session.get(Run, run.id)
    assert done.status == "ok" and done.trigger == "approve"
    assert _n(db_session, DataPoint, source_id=src.id) == 7
    assert _n(db_session, Change, run_id=run.id) == 3
    assert _n(db_session, PendingChange, run_id=run.id) == 0


def test_worker_leaves_approve_queued_when_source_busy(env, db_session, held_lock):
    src, _, sf = env
    run = _held(env)
    db_session.execute(Run.__table__.update().where(Run.id == run.id).values(status="approve_queued"))
    db_session.commit()
    held_lock(src.id)
    poll_queued_once(sf, None)
    db_session.expire_all()
    assert db_session.get(Run, run.id).status == "approve_queued"


def test_worker_failed_approve_returns_to_suspect_pending(env, db_session, monkeypatch):
    src, conn, sf = env
    run = _held(env)
    db_session.execute(Run.__table__.update().where(Run.id == run.id).values(status="approve_queued"))
    db_session.commit()

    def boom(raw):
        raise RuntimeError("bad file http://secret.example/x")

    monkeypatch.setattr(conn, "parse", boom)
    poll_queued_once(sf, None)
    db_session.expire_all()
    r = db_session.get(Run, run.id)
    assert r.status == "suspect_pending"
    assert "bad file" in r.error and "secret.example" not in r.error
    assert _n(db_session, PendingChange, run_id=run.id) == 3


def test_sweep_ignores_approve_queued(env, db_session):
    run = _held(env)
    db_session.execute(Run.__table__.update().where(Run.id == run.id).values(status="approve_queued"))
    db_session.commit()
    assert sweep_stale_runs(env[2]) == 0
    db_session.expire_all()
    assert db_session.get(Run, run.id).status == "approve_queued"


def test_reject_and_supersede_clear_pending(env, db_session):
    _, conn, sf = env
    a = _held(env)
    b = pipeline.run_source(sf, "fake")  # supersedes a
    db_session.expire_all()
    assert db_session.get(Run, a.id).status == "superseded"
    assert _n(db_session, PendingChange, run_id=a.id) == 0
    assert _n(db_session, PendingChange, run_id=b.id) == 3
    pipeline.reject_run(sf, b.id)
    assert _n(db_session, PendingChange, run_id=b.id) == 0
    # a good run supersedes a held one
    c = pipeline.run_source(sf, "fake")
    assert c.status == "suspect_pending"
    conn.rows = [raw(f"C{i}", "2020", i) for i in range(10)]
    assert pipeline.run_source(sf, "fake").status == "no_change"
    db_session.expire_all()
    assert db_session.get(Run, c.id).status == "superseded"
    assert _n(db_session, PendingChange, run_id=c.id) == 0


def test_changes_endpoint_reads_pending(client, env, db_session):
    run = _held(env)
    r = client.get(f"/api/runs/{run.id}/changes?change_type=removed&page_size=2").json()
    assert r["pending"] is True and r["total"] == 3 and len(r["items"]) == 2
    assert set(r["items"][0]) == {"change_type", "dataset", "indicator", "country", "period",
                                  "dims", "old", "new"}
    assert r["items"][0]["new"] is None and r["items"][0]["old"] is not None
    assert client.get(f"/api/runs/{run.id}/changes?country=C9").json()["total"] == 1
    client.post(f"/api/runs/{run.id}/approve")
    r = client.get(f"/api/runs/{run.id}/changes").json()
    assert r["pending"] is True and r["total"] == 3
    poll_queued_once(env[2], None)
    r = client.get(f"/api/runs/{run.id}/changes").json()
    assert "pending" not in r and r["total"] == 3


def _set_status(db_session, run_id, status):
    db_session.execute(Run.__table__.update().where(Run.id == run_id).values(status=status))
    db_session.commit()


def test_newer_good_run_supersedes_approve_queued_with_error(env, db_session):
    _, conn, sf = env
    run = _held(env)
    _set_status(db_session, run.id, "approve_queued")
    conn.rows = [raw(f"C{i}", "2020", i) for i in range(10)]
    assert pipeline.run_source(sf, "fake").status == "no_change"
    db_session.expire_all()
    r = db_session.get(Run, run.id)
    assert r.status == "superseded"
    assert r.error == "superseded by a newer run before the approval was applied"
    assert _n(db_session, PendingChange, run_id=run.id) == 0
    assert poll_queued_once(sf, None) == 0


def test_superseded_suspect_has_error(env, db_session):
    _, _, sf = env
    a = _held(env)
    pipeline.run_source(sf, "fake")
    db_session.expire_all()
    assert db_session.get(Run, a.id).error == "superseded by a newer run"


def test_prune_spares_approve_queued_raw_file(env, db_session, tmp_path):
    from app.core import raw_store
    src, _, _ = env
    paths = []
    for status in ("approve_queued", "ok", "ok"):
        f = tmp_path / f"{len(paths)}.json"
        f.write_text("x")
        paths.append(f)
        db_session.add(Run(source_id=src.id, status=status, raw_path=str(f)))
    db_session.commit()
    raw_store.prune(db_session, src.id, 1)  # keeps newest only; oldest is approve_queued
    assert paths[0].exists() and not paths[1].exists() and paths[2].exists()


def test_run_now_allowed_while_approve_queued(client, env, db_session):
    # Kept behaviour: approve_queued is not an "open" run for run-now; the source lock serialises.
    src, _, _ = env
    run = _held(env)
    _set_status(db_session, run.id, "approve_queued")
    assert client.post(f"/api/sources/{src.slug}/run").status_code == 202
