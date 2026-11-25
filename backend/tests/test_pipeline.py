import pytest
from sqlalchemy import func, select

from app.connectors import registry
from app.core import pipeline
from app.core.canonical import RawRow
from app.models import Change, DataPoint, Run, Source
from tests.conftest import FakeConnector


def raw(country, period, value):
    return RawRow("d", "i", country, period, value)


class RecordingNotifier:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def notify_run(self, run, source, top_indicators, streak, extras=None):
        if self.fail:
            raise RuntimeError("webhook down")
        self.calls.append((run.status, top_indicators, streak))


@pytest.fixture
def env(db_session, session_factory, monkeypatch):
    src = Source(slug="fake", name="Fake", area="test", access="manual")
    db_session.add(src)
    db_session.flush()
    conn = FakeConnector()
    monkeypatch.setitem(registry.CONNECTORS, "fake", conn)
    return src, conn, session_factory


def count_dp(db_session, src):
    return db_session.scalar(select(func.count()).select_from(DataPoint).where(DataPoint.source_id == src.id))


def test_first_run_is_baseline_without_changes(env, db_session):
    src, conn, sf = env
    conn.rows = [raw("AT", "2020", 1), raw("BE", "2020", 2)]
    n = RecordingNotifier()
    run = pipeline.run_source(sf, "fake", notifier=n)
    assert run.status == "baseline"
    assert count_dp(db_session, src) == 2
    assert db_session.scalar(select(func.count()).select_from(Change)) == 0
    assert len(n.calls) == 1


def test_second_identical_run_is_no_change_and_silent(env, db_session):
    src, conn, sf = env
    conn.rows = [raw("AT", "2020", 1)]
    pipeline.run_source(sf, "fake")
    n = RecordingNotifier()
    run = pipeline.run_source(sf, "fake", notifier=n)
    assert run.status == "no_change"
    assert [c[0] for c in n.calls] == ["no_change"]


def test_changes_are_recorded(env, db_session):
    src, conn, sf = env
    conn.rows = [raw("AT", "2020", 1), raw("BE", "2020", 2)]
    pipeline.run_source(sf, "fake")
    conn.rows = [raw("AT", "2020", 5), raw("BE", "2020", 2), raw("CZ", "2020", 3)]
    n = RecordingNotifier()
    run = pipeline.run_source(sf, "fake", notifier=n)
    assert run.status == "ok"
    assert (run.added, run.altered, run.removed) == (1, 1, 0)
    assert n.calls[0][1] == [("i", 2)]


def test_download_failure_leaves_data_untouched(env, db_session):
    src, conn, sf = env
    conn.rows = [raw("AT", "2020", 1)]
    pipeline.run_source(sf, "fake")
    conn.fail_download = True
    n = RecordingNotifier()
    run = pipeline.run_source(sf, "fake", notifier=n)
    assert run.status == "failed"
    assert "boom download" in run.error
    assert count_dp(db_session, src) == 1
    assert n.calls[0][2] == 1


def test_parse_failure_rolls_back(env, db_session):
    src, conn, sf = env
    conn.rows = [raw("AT", "2020", 1), raw("BE", "2020", 2), raw("CZ", "2020", 3)]
    conn.fail_parse_after = 2
    run = pipeline.run_source(sf, "fake")
    assert run.status == "failed"
    assert "boom parse" in run.error
    assert count_dp(db_session, src) == 0


def _baseline_ten(conn, sf):
    conn.rows = [raw(f"C{i}", "2020", i) for i in range(10)]
    pipeline.run_source(sf, "fake")
    conn.rows = conn.rows[:7]


def test_mass_removal_is_held(env, db_session):
    src, conn, sf = env
    _baseline_ten(conn, sf)
    run = pipeline.run_source(sf, "fake")
    assert run.status == "suspect_pending"
    assert count_dp(db_session, src) == 10


def test_approve_applies_latest_pending(env, db_session):
    src, conn, sf = env
    _baseline_ten(conn, sf)
    first = pipeline.run_source(sf, "fake")
    second = pipeline.run_source(sf, "fake")
    db_session.expire_all()
    assert db_session.get(Run, first.id).status == "superseded"
    with pytest.raises(ValueError):
        pipeline.approve_run(sf, first.id)
    approved = pipeline.approve_run(sf, second.id)
    assert approved.status == "ok"
    assert count_dp(db_session, src) == 7
    assert db_session.scalar(select(func.count()).select_from(Change)) == 3


def test_reject_marks_rejected(env, db_session):
    src, conn, sf = env
    _baseline_ten(conn, sf)
    r = pipeline.run_source(sf, "fake")
    assert pipeline.reject_run(sf, r.id).status == "rejected"
    assert count_dp(db_session, src) == 10


def test_notifier_error_is_recorded_not_raised(env, db_session):
    src, conn, sf = env
    conn.rows = [raw("AT", "2020", 1)]
    run = pipeline.run_source(sf, "fake", notifier=RecordingNotifier(fail=True))
    assert run.status == "baseline"
    assert "webhook down" in run.notify_error


def test_sync_sources_keeps_cron_and_enabled(env, db_session):
    src, conn, sf = env
    src.cron, src.enabled = "5 5 * * *", False
    db_session.flush()
    registry.sync_sources(db_session)
    db_session.refresh(src)
    assert (src.cron, src.enabled, src.name) == ("5 5 * * *", False, "Fake")


def test_prune_failure_leaves_run_ok(env, db_session, monkeypatch):
    src, conn, sf = env
    conn.rows = [raw("AT", "2020", 1)]
    pipeline.run_source(sf, "fake")
    conn.rows = [raw("AT", "2020", 2)]

    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(pipeline.raw_store, "prune", boom)
    run = pipeline.run_source(sf, "fake")
    assert run.status == "ok"
    assert run.error is None


def test_older_suspect_is_superseded_by_later_good_run(env, db_session):
    src, conn, sf = env
    _baseline_ten(conn, sf)
    a = pipeline.run_source(sf, "fake")
    assert a.status == "suspect_pending"
    conn.rows = [raw(f"C{i}", "2020", i) for i in range(10)]
    b = pipeline.run_source(sf, "fake")
    assert b.status == "no_change"
    db_session.expire_all()
    assert db_session.get(Run, a.id).status == "superseded"
    with pytest.raises(ValueError):
        pipeline.approve_run(sf, a.id)


def test_errors_are_redacted_and_truncated(env, db_session):
    src, conn, sf = env

    def leaky(client, dest):
        raise RuntimeError("POST https://mm.example.com/hooks/SECRET failed " + "x" * 5000)

    conn.download = leaky
    run = pipeline.run_source(sf, "fake")
    assert run.error.startswith("RuntimeError: POST <url> failed")
    assert "SECRET" not in run.error
    assert len(run.error) <= 2000

    n = RecordingNotifier()
    n.notify_run = lambda *a, **k: (_ for _ in ()).throw(ValueError("see https://h/SECRET"))
    conn.download = FakeConnector().download
    conn.rows = [raw("AT", "2020", 1)]
    run = pipeline.run_source(sf, "fake", notifier=n)
    assert run.notify_error == "ValueError: see <url>"


def test_prune_keeps_newest_and_spares_suspect(env, db_session, tmp_path):
    from pathlib import Path
    from app.core import raw_store

    src, conn, sf = env
    runs = []
    for i, status in enumerate(["ok", "suspect_pending", "ok", "ok", "ok"]):
        f = tmp_path / f"f{i}.json"
        f.write_text("x")
        r = Run(source_id=src.id, status=status, raw_path=str(f))
        db_session.add(r)
        runs.append(r)
    db_session.flush()
    raw_store.prune(db_session, src.id, 2)
    assert [Path(r.raw_path).exists() if r.raw_path else None for r in runs] == [
        None, True, None, True, True,
    ]


def test_parse_warnings_stored_in_run_error(env, db_session):
    src, conn, sf = env
    conn.rows = [raw("AT", "2020", 1)]
    pipeline.run_source(sf, "fake")
    conn.rows = [raw("AT", "2020", 2)]
    conn.warnings = ["incomplete: 1 indicators failed: X"]
    run = pipeline.run_source(sf, "fake")
    assert run.status == "ok"
    assert "incomplete: 1 indicators failed: X" in run.error


def test_approve_and_reject_refuse_when_source_busy(env, db_session, held_lock):
    src, conn, sf = env
    _baseline_ten(conn, sf)
    run = pipeline.run_source(sf, "fake")
    assert run.status == "suspect_pending"
    held_lock(src.id)
    with pytest.raises(pipeline.SourceBusyError):
        pipeline.approve_run(sf, run.id)
    with pytest.raises(pipeline.SourceBusyError):
        pipeline.reject_run(sf, run.id)
    db_session.expire_all()
    assert db_session.get(Run, run.id).status == "suspect_pending"
    assert count_dp(db_session, src) == 10


def test_second_approve_after_first_is_refused_and_lock_released(env, db_session, db_engine):
    src, conn, sf = env
    _baseline_ten(conn, sf)
    run = pipeline.run_source(sf, "fake")
    assert pipeline.approve_run(sf, run.id).trigger == "approve"
    with pytest.raises(ValueError):
        pipeline.approve_run(sf, run.id)
    with pytest.raises(ValueError):
        pipeline.reject_run(sf, run.id)
    assert db_session.scalar(select(func.count()).select_from(Change)) == 3
    with db_engine.connect() as c:  # lock was released on every path
        from sqlalchemy import text
        assert c.execute(text("SELECT pg_try_advisory_lock(:ns, :sid)"),
                         {"ns": pipeline.LOCK_NAMESPACE, "sid": src.id}).scalar()
        c.execute(text("SELECT pg_advisory_unlock(:ns, :sid)"),
                  {"ns": pipeline.LOCK_NAMESPACE, "sid": src.id})


def test_notifier_http_error_does_not_log_webhook_url(env, db_session, caplog):
    import httpx
    src, conn, sf = env
    conn.rows = [raw("AT", "2020", 1)]
    url = "https://mm.example.com/hooks/SECRETKEY"
    n = RecordingNotifier()

    def boom(*a):
        req = httpx.Request("POST", url)
        raise httpx.HTTPStatusError(f"500 for {url}", request=req,
                                    response=httpx.Response(500, request=req))

    n.notify_run = boom
    with caplog.at_level("DEBUG"):
        run = pipeline.run_source(sf, "fake", notifier=n)
    assert run.notify_error and "SECRETKEY" not in run.notify_error
    assert "hooks/" not in caplog.text and "SECRETKEY" not in caplog.text
    assert "notifier failed" in caplog.text


def test_failure_streak_counts_consecutive_failed_runs(env, db_session):
    src, conn, sf = env
    conn.rows = [raw("AT", "2020", 1)]
    pipeline.run_source(sf, "fake")
    conn.fail_download = True
    n = RecordingNotifier()
    for _ in range(3):
        pipeline.run_source(sf, "fake", notifier=n)
    assert [c[2] for c in n.calls] == [1, 2, 3]


def test_suspect_pending_does_not_upsert_indicators_until_approved(env, db_session):
    from app.models import Indicator
    src, conn, sf = env
    conn.rows = [raw(f"C{i}", "2020", i) for i in range(10)]
    pipeline.run_source(sf, "fake")
    db_session.execute(Indicator.__table__.update().where(Indicator.source_id == src.id)
                       .values(name="old name"))
    db_session.commit()
    conn.rows = conn.rows[:7]
    run = pipeline.run_source(sf, "fake")
    assert run.status == "suspect_pending"
    db_session.expire_all()
    name = select(Indicator.name).where(Indicator.source_id == src.id)
    assert db_session.scalar(name) == "old name"
    pipeline.approve_run(sf, run.id)
    db_session.expire_all()
    assert db_session.scalar(name) == "Indicator I"
