import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import inspect

from app.connectors.registry import CONNECTORS, resolve_connector
from app.models import Source
from app.worker import reload_jobs


def test_upgrade_creates_plan2_tables(db_engine):
    insp = inspect(db_engine)
    tables = set(insp.get_table_names())
    assert {"watch_targets", "watch_snapshots", "pending_changes", "change_rollup",
            "import_previews"} <= tables
    idx = insp.get_indexes("changes")
    assert any(i["column_names"] == ["source_id", "created_at"] for i in idx)


def test_resolve_connector_static_and_watch(db_session):
    from app.connectors.watch.connector import WatchConnector

    static_slug = next(iter(CONNECTORS))
    assert resolve_connector(db_session, static_slug) is CONNECTORS[static_slug]

    db_session.add(Source(slug="my-watch", name="My watch", area="custom", access="watch"))
    db_session.flush()
    c = resolve_connector(db_session, "my-watch")
    assert isinstance(c, WatchConnector)
    assert c.slug == "my-watch"
    assert c.name == "My watch"
    assert c.raw_suffix == ".json"
    assert c.access == "watch"
    assert c.area == "custom"
    assert callable(c.after_run)

    with pytest.raises(KeyError):
        resolve_connector(db_session, "nope")


def test_resolve_connector_non_watch_unregistered_source_raises(db_session):
    db_session.add(Source(slug="manual-x", name="M", area="a", access="manual"))
    db_session.flush()
    with pytest.raises(KeyError):
        resolve_connector(db_session, "manual-x")


def test_reload_jobs_schedules_watch_source(db_session, session_factory):
    db_session.add(Source(slug="w1", name="W1", area="custom", access="watch", cron="0 5 * * *"))
    db_session.add(Source(slug="junk", name="J", area="a", access="manual"))
    db_session.commit()
    sched = BackgroundScheduler(timezone="UTC")
    reload_jobs(sched, session_factory)
    assert sched.get_job("source:w1") is not None
    assert sched.get_job("source:junk") is None


def test_migration_0006_backfills_change_rollup(db_session, source, run):
    """Upgrading a DB whose changes predate change_rollup fills the rollup (idempotently)."""
    import importlib.util
    from datetime import datetime, timezone
    from decimal import Decimal

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    from app.models import Change
    from tests.conftest import BACKEND_DIR

    ts = datetime(2026, 1, 5, 23, 30, tzinfo=timezone.utc)
    for ctype in ("added", "altered", "altered"):
        db_session.add(Change(run_id=run.id, source_id=source.id, key_hash="k" * 40,
                              change_type=ctype, dataset="d", indicator="i", country="AT",
                              period="2020", new_num=Decimal(1), created_at=ts))
    db_session.flush()
    db_session.execute(text("DELETE FROM change_rollup"))
    path = BACKEND_DIR / "migrations" / "versions" / "0006_backfill_change_rollup.py"
    spec = importlib.util.spec_from_file_location("m0006", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for _ in range(2):  # idempotent
        with Operations.context(MigrationContext.configure(db_session.connection())):
            mod.upgrade()
    rows = db_session.execute(text(
        "SELECT indicator, day::text, added, altered, removed FROM change_rollup "
        "WHERE source_id = :s"), {"s": source.id}).all()
    assert [tuple(r) for r in rows] == [("i", "2026-01-05", 1, 2, 0)]
