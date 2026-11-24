import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.canonical import RawRow, to_canonical
from app.core.diff import apply_diff, stage_rows
from app.models import Run, Source

BACKEND_DIR = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def db_engine():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set")
    db_name = make_url(TEST_DATABASE_URL).database or ""
    if "test" not in db_name:
        raise RuntimeError(f"refusing to DROP SCHEMA on non-test database {db_name!r}")
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    """Session inside an outer transaction that is rolled back; commit() is safe."""
    connection = db_engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


def row(country, period, value):
    return to_canonical(RawRow("d", "i", country, period, value))


@pytest.fixture
def source(db_session):
    src = Source(slug="s", name="S", area="a", access="manual")
    db_session.add(src)
    db_session.flush()
    return src


@pytest.fixture
def run(db_session, source):
    r = Run(source_id=source.id, status="running")
    db_session.add(r)
    db_session.flush()
    return r


def seed(session, source, rows):
    """Baseline-load data_points through apply_diff without recording changes."""
    stage_rows(session, rows)
    apply_diff(session, source.id, 0, complete=True, record_changes=False)


class FakeConnector:
    slug = "fake"
    name = "Fake"
    area = "test"
    access = "manual"
    raw_suffix = ".json"

    def __init__(self, rows=None, complete=True, fail_download=False, fail_parse_after=None):
        self.rows = rows or []
        self.complete = complete
        self.fail_download = fail_download
        self.fail_parse_after = fail_parse_after

    def download(self, client, dest):
        if self.fail_download:
            raise RuntimeError("boom download")
        dest.write_text("raw")

    def parse(self, raw):
        from app.connectors.base import IndicatorMeta, ParseResult

        def gen():
            for n, r in enumerate(self.rows):
                if self.fail_parse_after is not None and n >= self.fail_parse_after:
                    raise RuntimeError("boom parse")
                yield r

        return ParseResult(
            rows=gen(), complete=self.complete,
            indicators=[IndicatorMeta(code="i", name="Indicator I", unit="u")],
            vintage="v1",
        )


@pytest.fixture
def session_factory(db_session, monkeypatch, tmp_path):
    """Sessions on the test connection; their commits stay inside the outer transaction."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "raw_dir", str(tmp_path))
    connection = db_session.get_bind()
    return lambda: Session(bind=connection, join_transaction_mode="create_savepoint")
