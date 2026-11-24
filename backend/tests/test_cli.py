import pytest

from app import cli
from app.connectors.registry import CONNECTORS
from app.models import Source
from app.core.canonical import RawRow
from tests.conftest import FakeConnector


def row(c, p, v):
    return RawRow("d", "i", c, p, v)


@pytest.fixture
def cli_env(db_session, session_factory, monkeypatch):
    c = FakeConnector(rows=[row("DEU", "2020", 1.0)])
    c.slug = "fa"
    monkeypatch.setitem(CONNECTORS, "fa", c)
    db_session.add(Source(slug="fa", name="fa", area="a", access="manual"))
    db_session.commit()
    monkeypatch.setattr(cli, "SessionLocal", session_factory)
    monkeypatch.setattr(cli, "notifier_from_settings", lambda: None)
    return c


def test_cli_run_prints_summary(cli_env, capsys):
    assert cli.main(["run", "fa"]) == 0
    assert capsys.readouterr().out.strip() == "fa: baseline rows=1 added=1 altered=0 removed=0"


def test_cli_run_failed_exits_nonzero(cli_env, capsys):
    cli_env.fail_download = True
    assert cli.main(["run", "fa"]) == 1
    assert capsys.readouterr().out.startswith("fa: failed")


def test_cli_from_raw_copies_file(cli_env, tmp_path, capsys):
    raw = tmp_path / "in.json"
    raw.write_text("from-file")
    seen = {}
    orig = cli_env.parse
    cli_env.parse = lambda p: (seen.setdefault("text", p.read_text()), orig(p))[1]
    assert cli.main(["run", "fa", "--from-raw", str(raw)]) == 0
    assert seen["text"] == "from-file"
    assert CONNECTORS["fa"] is cli_env  # registry restored after the run


def test_cli_sync_sources(session_factory, monkeypatch, db_session):
    monkeypatch.setattr(cli, "SessionLocal", session_factory)
    assert cli.main(["sync-sources"]) == 0
    assert db_session.query(Source).filter_by(slug="sipri-milex").count() == 1
