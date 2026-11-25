from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.connectors.registry import CONNECTORS
from app.core import pipeline
from app.core.pipeline import RunNotQueuedError, SourceBusyError, run_source
from app.models import Run, Source
from app.worker import build_scheduler, poll_queued_once, reload_jobs, sweep_stale_runs
from app.core.canonical import RawRow
from tests.conftest import FakeConnector


def row(c, p, v):
    return RawRow("d", "i", c, p, v)


@pytest.fixture
def fakes(monkeypatch):
    conns = {}
    for slug in ("fa", "fb", "fc"):
        c = FakeConnector(rows=[row("DEU", "2020", 1.0)])
        c.slug = slug
        conns[slug] = c
        monkeypatch.setitem(CONNECTORS, slug, c)
    return conns


def _add_sources(db_session, specs):
    out = {}
    for slug, cron, enabled in specs:
        s = Source(slug=slug, name=slug, area="a", access="manual", cron=cron, enabled=enabled)
        db_session.add(s)
        out[slug] = s
    db_session.commit()
    return out


def _cron_str(job):
    return str(job.trigger)


def test_jobs_match_enabled_sources(db_session, session_factory, fakes):
    _add_sources(db_session, [("fa", "0 3 * * *", True), ("fb", "30 4 * * 1", True),
                              ("fc", "0 5 * * *", False)])
    sched = build_scheduler(session_factory, None)
    try:
        jobs = {j.id: j for j in sched.get_jobs() if j.id.startswith("source:")}
        assert set(jobs) == {"source:fa", "source:fb"}
        expected = CronTrigger.from_crontab("30 4 * * 1", timezone="UTC")
        assert _cron_str(jobs["source:fb"]) == str(expected)
        assert jobs["source:fa"].max_instances == 1
        assert jobs["source:fa"].coalesce is True
        assert jobs["source:fa"].misfire_grace_time == 3600
    finally:
        if sched.running:
            sched.shutdown(wait=False)


def test_source_without_connector_is_skipped(db_session, session_factory, fakes):
    _add_sources(db_session, [("fa", "0 3 * * *", True), ("nope", "0 3 * * *", True)])
    sched = build_scheduler(session_factory, None)
    assert {j.id for j in sched.get_jobs() if j.id.startswith("source:")} == {"source:fa"}


def test_reload_picks_up_changes(db_session, session_factory, fakes):
    srcs = _add_sources(db_session, [("fa", "0 3 * * *", True), ("fb", "0 4 * * *", True)])
    sched = BackgroundScheduler(timezone="UTC")
    reload_jobs(sched, session_factory)
    assert sched.get_job("source:fa") is not None
    srcs["fa"].cron = "15 6 * * *"
    srcs["fb"].enabled = False
    db_session.commit()
    reload_jobs(sched, session_factory)
    assert _cron_str(sched.get_job("source:fa")) == str(
        CronTrigger.from_crontab("15 6 * * *", timezone="UTC"))
    assert sched.get_job("source:fb") is None
    # unchanged cron is not rescheduled
    stamp = sched.get_job("source:fa").trigger
    reload_jobs(sched, session_factory)
    assert sched.get_job("source:fa").trigger is stamp
    srcs["fb"].enabled = True
    db_session.commit()
    reload_jobs(sched, session_factory)
    assert sched.get_job("source:fb") is not None


def test_queued_run_is_executed(db_session, session_factory, fakes):
    srcs = _add_sources(db_session, [("fa", "0 3 * * *", True)])
    q = Run(source_id=srcs["fa"].id, status="queued", trigger="manual")
    db_session.add(q)
    db_session.commit()
    qid = q.id
    assert poll_queued_once(session_factory, None) == 1
    db_session.expire_all()
    done = db_session.get(Run, qid)
    assert done.status == "baseline"
    assert done.trigger == "manual"
    assert db_session.query(Run).filter_by(source_id=srcs["fa"].id).count() == 1
    assert poll_queued_once(session_factory, None) == 0


def test_poll_survives_failure(db_session, session_factory, fakes):
    srcs = _add_sources(db_session, [("fa", "0 3 * * *", True)])
    fakes["fa"].fail_download = True
    q = Run(source_id=srcs["fa"].id, status="queued", trigger="manual")
    db_session.add(q)
    db_session.commit()
    qid = q.id
    poll_queued_once(session_factory, None)
    db_session.expire_all()
    assert db_session.get(Run, qid).status == "failed"


def test_sweep_stale_runs(db_session, session_factory, source):
    r1 = Run(source_id=source.id, status="running")
    r2 = Run(source_id=source.id, status="ok")
    r3 = Run(source_id=source.id, status="queued", trigger="manual")
    db_session.add_all([r1, r2, r3])
    db_session.commit()
    assert sweep_stale_runs(session_factory) == 1
    db_session.expire_all()
    assert r1.status == "failed"
    assert r1.error == "worker restarted while running"
    assert r1.finished_at is not None
    assert r2.status == "ok" and r3.status == "queued"


def test_sweep_skips_run_whose_source_lock_is_held(db_session, session_factory, source, held_lock):
    r = Run(source_id=source.id, status="running")
    db_session.add(r)
    db_session.commit()
    held_lock(source.id)
    assert sweep_stale_runs(session_factory) == 0
    db_session.expire_all()
    assert r.status == "running"


def test_busy_source_raises_and_creates_no_run(db_session, session_factory, fakes, held_lock):
    srcs = _add_sources(db_session, [("fa", "0 3 * * *", True)])
    held_lock(srcs["fa"].id)
    with pytest.raises(SourceBusyError):
        run_source(session_factory, "fa")
    assert db_session.query(Run).filter_by(source_id=srcs["fa"].id).count() == 0


def test_busy_source_leaves_queued_run_queued(db_session, session_factory, fakes, held_lock):
    srcs = _add_sources(db_session, [("fa", "0 3 * * *", True)])
    q = Run(source_id=srcs["fa"].id, status="queued", trigger="manual")
    db_session.add(q)
    db_session.commit()
    held_lock(srcs["fa"].id)
    assert poll_queued_once(session_factory, None) == 0
    db_session.expire_all()
    assert q.status == "queued"


def test_lock_released_after_run(db_session, session_factory, fakes, db_engine):
    srcs = _add_sources(db_session, [("fa", "0 3 * * *", True)])
    run_source(session_factory, "fa")
    with db_engine.connect() as c:
        assert c.execute(text("SELECT pg_try_advisory_lock(:ns, :sid)"),
                         {"ns": pipeline.LOCK_NAMESPACE, "sid": srcs["fa"].id}).scalar()
        c.execute(text("SELECT pg_advisory_unlock(:ns, :sid)"),
                  {"ns": pipeline.LOCK_NAMESPACE, "sid": srcs["fa"].id})


def test_lost_claim_raises_and_poller_does_not_fail_run(db_session, session_factory, fakes):
    srcs = _add_sources(db_session, [("fa", "0 3 * * *", True)])
    r = Run(source_id=srcs["fa"].id, status="running", trigger="manual")
    db_session.add(r)
    db_session.commit()
    with pytest.raises(RunNotQueuedError):
        run_source(session_factory, "fa", run_id=r.id)
    db_session.expire_all()
    assert r.status == "running"  # untouched


def test_poller_skips_run_claimed_between_list_and_claim(db_session, session_factory, fakes, monkeypatch):
    srcs = _add_sources(db_session, [("fa", "0 3 * * *", True)])
    q = Run(source_id=srcs["fa"].id, status="queued", trigger="manual")
    db_session.add(q)
    db_session.commit()
    real = pipeline.run_source

    def racing(sf, slug, **kw):
        db_session.execute(text("UPDATE runs SET status='running' WHERE id=:i"), {"i": kw["run_id"]})
        db_session.commit()
        return real(sf, slug, **kw)

    monkeypatch.setattr("app.worker.run_source", racing)
    assert poll_queued_once(session_factory, None) == 0
    db_session.expire_all()
    assert q.status == "running"
