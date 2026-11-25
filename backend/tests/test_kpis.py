from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.api import deps
from app.api.main import create_app
from app.config import get_settings
from app.core import rollup
from app.models import Change, ChangeRollup, DataPoint, Indicator, Run, Source
from tests.conftest import row, seed


@pytest.fixture
def client(db_session, session_factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "dashboard_password", "secret")
    app = create_app()
    app.dependency_overrides[deps.get_session] = lambda: db_session
    app.dependency_overrides[deps.get_session_factory] = lambda: session_factory
    c = TestClient(app)
    c.auth = ("u", "secret")
    return c


def _change(session, source, run, indicator="i", day=None, ctype="altered", country="AT",
            period="2020", old=1, new=2):
    ts = datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)
    session.add(Change(
        run_id=run.id, source_id=source.id, key_hash="k" * 40, change_type=ctype, dataset="d",
        indicator=indicator, country=country, period=period, old_num=Decimal(old),
        new_num=Decimal(new), created_at=ts,
    ))
    session.flush()


def _ind(session, source, code, name=None, unit=None):
    session.add(Indicator(source_id=source.id, code=code, name=name or code, unit=unit))
    session.flush()


def _today():
    return datetime.now(timezone.utc).date()


def test_rollup_counts_per_day(db_session, source, run):
    d = _today() - timedelta(days=3)
    _change(db_session, source, run, day=d, ctype="added")
    _change(db_session, source, run, day=d, ctype="altered", country="BE")
    _change(db_session, source, run, day=d, ctype="altered", country="CH")
    _change(db_session, source, run, day=d + timedelta(days=1), ctype="removed")
    rollup.refresh_rollup(db_session, run.id)
    rows = {r.day: (r.added, r.altered, r.removed)
            for r in db_session.scalars(select(ChangeRollup).where(ChangeRollup.source_id == source.id))}
    assert rows == {d: (1, 2, 0), d + timedelta(days=1): (0, 0, 1)}
    # backfill rebuilds from all changes and is idempotent
    assert rollup.backfill(db_session) >= 2
    again = {r.day: (r.added, r.altered, r.removed)
             for r in db_session.scalars(select(ChangeRollup).where(ChangeRollup.source_id == source.id))}
    assert again == rows


def test_kpis_frequency_fields(client, db_session, source, run):
    _ind(db_session, source, "i", "Indicator I", "u")
    d = _today() - timedelta(days=45)
    for off in (0, 10, 30):
        _change(db_session, source, run, day=d + timedelta(days=off))
    rollup.backfill(db_session)
    body = client.get("/api/kpis", params={"source": "s"}).json()
    assert body["total"] == 1
    it = body["items"][0]
    assert it["source"] == "s" and it["indicator"] == "i" and it["name"] == "Indicator I"
    assert it["unit"] == "u"
    assert it["typical_interval_days"] == 15
    assert it["changes_all"] == 3
    assert it["changes_12m"] == 3
    assert it["changes_30d"] == 1
    assert it["last_changed"].startswith((d + timedelta(days=30)).isoformat())
    assert it["first_seen"]
    assert len(it["sparkline"]) == 12 and sum(it["sparkline"]) == 3


def test_typical_interval_null_with_one_day(client, db_session, source, run):
    _ind(db_session, source, "i")
    d = _today() - timedelta(days=2)
    _change(db_session, source, run, day=d)
    _change(db_session, source, run, day=d, country="BE")
    rollup.backfill(db_session)
    it = client.get("/api/kpis").json()["items"][0]
    assert it["typical_interval_days"] is None
    assert it["changes_all"] == 2


def test_kpis_sort_and_filter(client, db_session, source, run):
    other = Source(slug="o", name="O", area="a", access="manual")
    db_session.add(other)
    db_session.flush()
    _ind(db_session, source, "alpha", "Alpha GDP")
    _ind(db_session, source, "beta", "Beta pop")
    _ind(db_session, source, "quiet", "Quiet")  # no changes at all
    _ind(db_session, other, "gamma", "Gamma GDP")
    t = _today()
    _change(db_session, source, run, indicator="alpha", day=t - timedelta(days=1))
    for off in (5, 6, 7):
        _change(db_session, source, run, indicator="beta", day=t - timedelta(days=off))
    rollup.backfill(db_session)

    items = client.get("/api/kpis", params={"source": "s", "sort": "changes_30d"}).json()["items"]
    assert [i["indicator"] for i in items] == ["beta", "alpha", "quiet"]
    assert items[2]["changes_all"] == 0 and items[2]["sparkline"] == [0] * 12
    assert items[2]["last_changed"] is None
    items = client.get("/api/kpis", params={"source": "s", "sort": "changes_30d", "order": "asc"}).json()["items"]
    assert items[0]["indicator"] == "quiet"
    items = client.get("/api/kpis", params={"source": "s", "sort": "last_changed"}).json()["items"]
    assert [i["indicator"] for i in items][:2] == ["alpha", "beta"]
    body = client.get("/api/kpis", params={"q": "gdp"}).json()
    assert body["total"] == 2 and {i["indicator"] for i in body["items"]} == {"alpha", "gamma"}
    body = client.get("/api/kpis", params={"page_size": 1, "page": 2, "sort": "changes_30d"}).json()
    assert body["total"] == 4 and len(body["items"]) == 1
    assert client.get("/api/kpis", params={"sort": "bogus"}).status_code == 422
    assert client.get("/api/kpis", params={"source": "nope"}).status_code == 404


def test_series_points_and_history(client, db_session, source, run):
    seed(db_session, source, [row("AT", "2021", 5), row("AT", "2020", "2.50"), row("BE", "2020", 9)])
    t = _today()
    _change(db_session, source, run, day=t - timedelta(days=5), old=1, new=2.5, period="2020")
    _change(db_session, source, run, day=t - timedelta(days=1), ctype="added", period="2021")
    _change(db_session, source, run, day=t, country="BE")
    r = client.get("/api/sources/s/series", params={"indicator": "i", "country": "AT"})
    assert r.status_code == 200
    body = r.json()
    assert [(p["period"], p["value"]) for p in body["points"]] == [("2020", "2.5"), ("2021", "5")]
    assert [h["period"] for h in body["history"]] == ["2021", "2020"]  # newest first, AT only
    h = body["history"][1]
    assert h["run_id"] == run.id and h["change_type"] == "altered"
    assert h["old"] == "1" and h["new"] == "2.5" and h["created_at"]
    assert body["attribution"] is None
    assert client.get("/api/sources/s/series", params={"indicator": "i"}).status_code == 422


def test_series_history_capped(client, db_session, source, run):
    seed(db_session, source, [row("AT", "2020", 1)])
    t = _today()
    db_session.execute(
        text("""INSERT INTO changes (run_id, source_id, key_hash, change_type, dataset, indicator,
                country, period, new_num)
                SELECT :r, :s, repeat('k', 40), 'altered', 'd', 'i', 'AT', '2020', g
                FROM generate_series(1, 520) g"""),
        {"r": run.id, "s": source.id},
    )
    body = client.get("/api/sources/s/series", params={"indicator": "i", "country": "AT"}).json()
    assert len(body["history"]) == 500


def test_explorer_indicators_and_countries(client, db_session, source, run):
    seed(db_session, source, [row("AT", "2020", 1), row("AT", "2021", 1), row("BE", "2020", 2)])
    _ind(db_session, source, "i", "Indicator I", "u")
    _ind(db_session, source, "z", "Zero")
    body = client.get("/api/sources/s/indicators").json()
    assert body["total"] == 2
    assert {(i["code"], i["points"]) for i in body["items"]} == {("i", 3), ("z", 0)}
    assert client.get("/api/sources/s/indicators", params={"q": "zer"}).json()["total"] == 1
    body = client.get("/api/sources/s/indicators/i/countries").json()
    assert body["total"] == 2
    by = {c["country"]: c for c in body["items"]}
    assert by["AT"]["points"] == 2 and by["BE"]["points"] == 1 and by["AT"]["last_changed"]
    assert client.get("/api/sources/s/indicators/i/countries", params={"q": "be"}).json()["total"] == 1
    assert client.get("/api/sources/nope/indicators").status_code == 404


def test_sources_have_attribution_and_incomplete(client, db_session, source):
    from app.connectors import registry
    wb = Source(slug="worldbank-wdi", name="WB", area="a", access="api")
    db_session.add(wb)
    db_session.flush()
    db_session.add(Run(source_id=source.id, status="ok", finished_at=datetime.now(timezone.utc),
                       error="incomplete: sheet missing"))
    db_session.add(Run(source_id=source.id, status="running"))
    db_session.add(Run(source_id=wb.id, status="failed", finished_at=datetime.now(timezone.utc),
                        error="incomplete: boom"))
    db_session.flush()
    items = {i["slug"]: i for i in client.get("/api/sources").json()}
    assert items["s"]["attribution"] is None and items["s"]["incomplete"] is True
    assert items["s"]["noisy_targets"] == 0
    assert items["worldbank-wdi"]["attribution"] == \
        "Source: World Bank, World Development Indicators (CC BY 4.0)"
    assert items["worldbank-wdi"]["incomplete"] is False
    assert registry.CONNECTORS["imf-weo"].attribution == "Source: IMF DataMapper"
    assert registry.CONNECTORS["sipri-milex"].attribution == \
        "Source: SIPRI Military Expenditure Database"


def test_indicators_first_seen_set_on_insert_only(db_session, source):
    from app.connectors.base import IndicatorMeta
    from app.core.pipeline import _upsert_indicators
    _upsert_indicators(db_session, source.id, [IndicatorMeta(code="i", name="A", unit="u")])
    first = db_session.scalar(text("SELECT first_seen_at FROM indicators"))
    db_session.execute(text("UPDATE indicators SET first_seen_at = '2020-01-01'"))
    _upsert_indicators(db_session, source.id, [IndicatorMeta(code="i", name="B", unit="u")])
    row_ = db_session.execute(text("SELECT name, first_seen_at FROM indicators")).one()
    assert row_.name == "B" and row_.first_seen_at.year == 2020
    assert first is not None


def test_pipeline_refreshes_rollup(db_session, session_factory, monkeypatch):
    from app.connectors import registry
    from app.core import pipeline
    from app.core.canonical import RawRow
    from tests.conftest import FakeConnector

    src = Source(slug="fake", name="Fake", area="test", access="manual")
    db_session.add(src)
    db_session.flush()
    conn = FakeConnector()
    monkeypatch.setitem(registry.CONNECTORS, "fake", conn)
    conn.rows = [RawRow("d", "i", "AT", "2020", 1)]
    assert pipeline.run_source(session_factory, "fake").status == "baseline"
    conn.rows = [RawRow("d", "i", "AT", "2020", 2), RawRow("d", "i", "BE", "2020", 3)]
    assert pipeline.run_source(session_factory, "fake").status == "ok"
    total = db_session.execute(
        text("SELECT sum(added), sum(altered), sum(removed) FROM change_rollup WHERE source_id=:s"),
        {"s": src.id}).one()
    assert tuple(total) == (1, 1, 0)


def test_refresh_rollup_is_idempotent(db_session, source, run):
    d = _today() - timedelta(days=3)
    _change(db_session, source, run, day=d, ctype="added")
    _change(db_session, source, run, day=d, country="BE")
    rollup.refresh_rollup(db_session, run.id)
    rollup.refresh_rollup(db_session, run.id)
    r = db_session.scalars(select(ChangeRollup).where(ChangeRollup.source_id == source.id)).one()
    assert (r.added, r.altered, r.removed) == (1, 1, 0)


def test_refresh_rollup_recomputes_full_day_across_runs(db_session, source, run):
    d = _today() - timedelta(days=3)
    run2 = Run(source_id=source.id, status="ok")
    db_session.add(run2)
    db_session.flush()
    _change(db_session, source, run, day=d)
    rollup.refresh_rollup(db_session, run.id)
    _change(db_session, source, run2, day=d, country="BE")
    rollup.refresh_rollup(db_session, run2.id)
    rollup.refresh_rollup(db_session, run.id)
    r = db_session.scalars(select(ChangeRollup).where(ChangeRollup.source_id == source.id)).one()
    assert r.altered == 2


def test_approve_run_path_fills_rollup(db_session, session_factory, monkeypatch):
    from app.connectors import registry
    from app.core import pipeline
    from app.core.canonical import RawRow
    from tests.conftest import FakeConnector

    src = Source(slug="fake", name="Fake", area="test", access="manual")
    db_session.add(src)
    db_session.commit()
    conn = FakeConnector()
    monkeypatch.setitem(registry.CONNECTORS, "fake", conn)
    conn.rows = [RawRow("d", "i", f"C{i}", "2020", i) for i in range(10)]
    pipeline.run_source(session_factory, "fake")
    conn.rows = conn.rows[:7]
    held = pipeline.run_source(session_factory, "fake")
    assert held.status == "suspect_pending"
    n = db_session.execute(text("SELECT count(*) FROM change_rollup")).scalar()
    assert n == 0  # held runs record nothing
    db_session.execute(text("UPDATE runs SET status='approve_queued' WHERE id=:i"), {"i": held.id})
    db_session.commit()
    done = pipeline.approve_run(session_factory, held.id)
    assert done.status == "ok"
    total = db_session.execute(
        text("SELECT sum(added), sum(altered), sum(removed) FROM change_rollup WHERE source_id=:s"),
        {"s": src.id}).one()
    assert tuple(total) == (0, 0, 3)


def test_sparkline_month_buckets_across_year_boundary(client, db_session, source, run, monkeypatch):
    from app.api import routes_kpis
    monkeypatch.setattr(routes_kpis, "_today", lambda: date(2026, 2, 10))
    _ind(db_session, source, "i")
    _change(db_session, source, run, day=date(2025, 12, 31))   # 3rd from last
    _change(db_session, source, run, day=date(2026, 1, 1), country="BE")
    _change(db_session, source, run, day=date(2026, 1, 20), country="CH")
    _change(db_session, source, run, day=date(2025, 3, 5), country="DE")   # first bucket
    _change(db_session, source, run, day=date(2025, 2, 28), country="FR")  # outside window
    rollup.backfill(db_session)
    it = client.get("/api/kpis").json()["items"][0]
    # window: Mar 2025 .. Feb 2026
    assert it["sparkline"] == [1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 2, 0]
    assert it["changes_12m"] == 4
