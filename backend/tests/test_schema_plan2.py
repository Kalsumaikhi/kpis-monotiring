import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import func, inspect, select

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


def _load_migration(filename: str, modname: str):
    import importlib.util

    from tests.conftest import BACKEND_DIR

    spec = importlib.util.spec_from_file_location(
        modname, BACKEND_DIR / "migrations" / "versions" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_0010_dedupes_kpi_names_then_adds_unique_index(db_session, caplog):
    """Duplicate KPI names in one source (case-insensitive) get " (2)", " (3)" in id order.
    A renamed KPI that alone held its exact name keeps its rows, re-keyed to the new name; for
    an exact-name clash the shared rows are dropped and every KPI that used it baselines."""
    import logging

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from app.core.canonical import dims_to_json, key_hash_for
    from app.models import DataPoint, Indicator, PendingChange, Run, WatchTarget

    mod = _load_migration("0010_unique_kpi_names.py", "m0010")
    ops = lambda: Operations.context(MigrationContext.configure(db_session.connection()))  # noqa: E731
    with ops():
        mod.downgrade()  # back to the 0009 schema: duplicates are possible again
    a = Source(slug="dup-a", name="A", area="custom", access="watch")
    b = Source(slug="dup-b", name="B", area="custom", access="watch")
    db_session.add_all([a, b])
    db_session.flush()
    t = [WatchTarget(source_id=a.id, kpi_name=n, url=f"https://h.example/{i}", needs_baseline=False)
         for i, n in enumerate(["Price", "PRICE", "price (2)", "Price", "Other"])]
    t.append(WatchTarget(source_id=b.id, kpi_name="Price", url="https://h.example/b",
                         needs_baseline=False))
    db_session.add_all(t)
    db_session.flush()
    for code in ("Price", "PRICE", "Other"):
        db_session.add(Indicator(source_id=a.id, code=code, name=code))
        for ds, dims in (("page", {}), ("api", {"path": "x.y"})):
            db_session.add(DataPoint(
                source_id=a.id, key_hash=key_hash_for(ds, code, "", "", dims_to_json(dims)),
                dataset=ds, indicator=code, country="", period="", dims=dims, value_text="h",
                value_hash="h" * 40))
    held = Run(source_id=a.id, status="suspect_pending")
    queued = Run(source_id=a.id, status="approve_queued")
    db_session.add_all([held, queued])
    db_session.flush()
    db_session.add(PendingChange(run_id=held.id, source_id=a.id, key_hash="k" * 40,
                                 change_type="altered", dataset="page", indicator="Price",
                                 country="", period=""))
    db_session.flush()

    with caplog.at_level(logging.INFO), ops():
        mod.upgrade()
    db_session.expire_all()
    names = [(x.kpi_name, x.needs_baseline) for x in (db_session.get(WatchTarget, y.id) for y in t)]
    assert names == [("Price", True), ("PRICE (3)", False), ("price (2)", False),
                     ("Price (4)", True), ("Other", False), ("Price", False)]
    assert "PRICE (3)" in caplog.text and "Price (4)" in caplog.text
    # "PRICE" had one owner: its rows follow it, with the key recomputed as a rename does.
    # "Price" was shared by two KPIs: its rows are dropped. "Other" stays.
    left = db_session.execute(select(DataPoint.indicator, DataPoint.dataset, DataPoint.key_hash,
                                     DataPoint.dims).where(DataPoint.source_id == a.id)).all()
    assert sorted((i, ds) for i, ds, _, _ in left) == [
        ("Other", "api"), ("Other", "page"), ("PRICE (3)", "api"), ("PRICE (3)", "page")]
    for i, ds, kh, dims in left:
        assert kh == key_hash_for(ds, i, "", "", dims_to_json(dims))
    assert sorted(db_session.scalars(select(Indicator.code).where(Indicator.source_id == a.id))) \
        == ["Other", "PRICE (3)"]
    # held runs are superseded with the same wording as _supersede_held
    db_session.refresh(held)
    db_session.refresh(queued)
    assert held.status == queued.status == "superseded"
    assert held.error == "superseded by a change to the source's KPIs"
    assert queued.error == "superseded by a change to the source's KPIs before the approval was applied"
    assert db_session.scalar(select(func.count()).select_from(PendingChange)
                             .where(PendingChange.run_id == held.id)) == 0
    # the index now refuses a case-insensitive duplicate
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.execute(text(
                "INSERT INTO watch_targets (source_id, kpi_name, url) VALUES (:s, 'OTHER', 'https://h.example/z')"),
                {"s": a.id})
    with ops():
        mod.upgrade()  # idempotent on a clean table: nothing renamed, the index is kept


def test_migration_0010_renames_are_shown_by_the_alembic_cli(db_engine):
    """`alembic upgrade head` shows each rename: through alembic's logging (env.py configures
    it from alembic.ini) and on stderr."""
    import os
    import subprocess
    import sys

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from tests.conftest import BACKEND_DIR

    base = make_url(db_engine.url.render_as_string(hide_password=False))
    name = f"{base.database}_m10"
    url = base.set(database=name)
    with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        c.execute(text(f"CREATE DATABASE {name}"))
    env = {**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)}

    def alembic(*args):
        r = subprocess.run([sys.executable, "-m", "alembic", *args], env=env, cwd=str(BACKEND_DIR),
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stderr

    e = create_engine(url)
    try:
        alembic("upgrade", "0009")
        with e.begin() as c:
            sid = c.execute(text("INSERT INTO sources (slug, name, area, access) "
                                 "VALUES ('w', 'W', 'custom', 'watch') RETURNING id")).scalar_one()
            for i, n in enumerate(("Price", "PRICE")):
                c.execute(text("INSERT INTO watch_targets (source_id, kpi_name, url) "
                               "VALUES (:s, :n, :u)"), {"s": sid, "n": n, "u": f"https://h.example/{i}"})
        err = alembic("upgrade", "head")
        assert "Running upgrade 0009 -> 0010" in err  # alembic's own INFO logging is configured
        assert "[alembic.runtime.migration]" in err and "'PRICE (2)'" in err
        assert err.count("'PRICE (2)'") == 1  # shown once: through alembic's logging, not printed twice
    finally:
        e.dispose()
        with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))


def test_migration_0011_downgrade_and_upgrade(db_session):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    mod = _load_migration("0011_source_needs_baseline_at.py", "m0011")
    has = text("SELECT 1 FROM information_schema.columns WHERE table_name = 'sources' "
               "AND column_name = 'needs_baseline_at'")
    with Operations.context(MigrationContext.configure(db_session.connection())):
        mod.downgrade()
        assert db_session.execute(has).first() is None
        mod.upgrade()
        assert db_session.execute(has).first() is not None
