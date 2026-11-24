from datetime import datetime, timedelta, timezone

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.connectors.registry import CONNECTORS
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
    before = sched.get_job("source:fa")
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
    assert before is not None
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
