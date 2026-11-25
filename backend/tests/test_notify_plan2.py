import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.main import create_app
from app.config import get_settings
from app.connectors.registry import CONNECTORS
from app.core import pipeline
from app.core.canonical import RawRow
from app.models import Run, Source, WatchSnapshot, WatchTarget
from app.notify.mattermost import format_run_message
from tests.conftest import FakeConnector

BASE = "http://dash"


def fmt(run, source, top=(), streak=0, extras=None):
    return format_run_message(run, source, list(top), streak, dashboard_base_url=BASE,
                              consecutive_failure_alert=3, extras=extras)


def mk_run(**kw):
    f = dict(id=7, source_id=1, status="ok", vintage=None, rows_total=10, added=0,
             altered=2, removed=0, error=None)
    f.update(kw)
    return Run(**f)


def test_watch_page_message_format():
    src = Source(slug="x", name="My Source", area="custom", access="watch")
    extras = {"page_kpis": ["GDP page", "Budget page"],
              "excerpt": {"added": ["new line"], "removed": ["old line"]},
              "incomplete_note": None, "api_counts": (0, 0, 0)}
    assert fmt(mk_run(), src, extras=extras) == (
        "**My Source** — 2 KPI page(s) updated: GDP page, Budget page\n"
        "+ new line\n- old line\n[View changes](http://dash/runs/7)")


def test_watch_message_caps_names_and_lines_and_joins_api_block():
    src = Source(slug="x", name="S", area="custom", access="watch")
    names = [f"K{i}" for i in range(7)]
    extras = {"page_kpis": names,
              "excerpt": {"added": [f"a{i}" for i in range(8)], "removed": ["r"]},
              "incomplete_note": None, "api_counts": (3, 0, 0)}
    text = fmt(mk_run(added=3, altered=7), src, top=[("p", 3)], extras=extras)
    page, api = text.split("\n\n")
    assert page.startswith("**S** — 7 KPI page(s) updated: K0, K1, K2, K3, K4\n")
    assert page.count("\n+ ") == 5 and "+ a4" in page and "+ a5" not in page
    assert api == ("**S** — 3 rows added · 0 values revised · 0 removed\n"
                   "Top indicators: p (3)\n[View changes](http://dash/runs/7)")


def test_incomplete_note_appended_or_alone():
    src = Source(slug="x", name="S", area="a", access="api")
    note = "2 indicators failed"
    both = fmt(mk_run(altered=1), src, extras={"incomplete_note": note})
    assert both.endswith("\n\n:warning: **S** — incomplete fetch: 2 indicators failed")
    assert both.startswith("**S** — 0 rows added")
    alone = fmt(mk_run(status="no_change", altered=0), src, extras={"incomplete_note": "x" * 300})
    assert alone == ":warning: **S** — incomplete fetch: " + "x" * 200
    base = fmt(mk_run(status="baseline"), src, extras={"incomplete_note": note})
    assert base.endswith(":warning: **S** — incomplete fetch: 2 indicators failed")
    assert fmt(mk_run(status="no_change"), src, extras=None) is None


class Rec:
    def __init__(self):
        self.calls = []

    def notify_run(self, run, source, top, streak, extras=None):
        self.calls.append((run.status, extras))


def test_incomplete_note_only_on_transition(db_session, session_factory, monkeypatch):
    c = FakeConnector(rows=[RawRow("d", "i", "DEU", "2020", 1.0)])
    c.slug = "inc"
    monkeypatch.setitem(CONNECTORS, "inc", c)
    db_session.add(Source(slug="inc", name="Inc", area="a", access="manual"))
    db_session.commit()
    rec = Rec()
    pipeline.run_source(session_factory, "inc", notifier=rec)  # baseline, complete
    assert rec.calls[-1][1]["incomplete_note"] is None
    c.warnings = ["incomplete: 1 sheet missing"]
    pipeline.run_source(session_factory, "inc", notifier=rec)  # no_change, now incomplete
    assert rec.calls[-1][1]["incomplete_note"] == "1 sheet missing"
    pipeline.run_source(session_factory, "inc", notifier=rec)  # still incomplete
    assert rec.calls[-1][1]["incomplete_note"] is None
    c.warnings = []
    pipeline.run_source(session_factory, "inc", notifier=rec)
    c.warnings = ["incomplete: again"]
    pipeline.run_source(session_factory, "inc", notifier=rec)  # new transition
    assert rec.calls[-1][1]["incomplete_note"] == "again"


def _page(i, name, text):
    return {"id": i, "kpi_name": name, "url": f"https://h/{name}", "kind": "page", "status": "ok",
            "content_type": "text/html", "text": text,
            "sha256": hashlib.sha256(text.encode()).hexdigest(), "error": None}


@pytest.fixture
def pages(db_session, session_factory, monkeypatch, tmp_path):
    """A watch source with 3 page targets whose raw file is fed via from_raw."""
    src = Source(slug="custom-p", name="P", area="custom", access="watch")
    db_session.add(src)
    db_session.flush()
    ts = [WatchTarget(source_id=src.id, kpi_name=n, url=f"https://h/{n}", kind="page")
          for i, n in enumerate(["A", "B", "C"])]
    db_session.add_all(ts)
    db_session.flush()
    db_session.commit()

    def feed(texts):
        p = tmp_path / f"in{len(list(tmp_path.iterdir()))}.json"
        p.write_text(json.dumps({"targets": [_page(t.id, t.kpi_name, x)
                                             for t, x in zip(ts, texts)]}))
        return p
    return src, ts, feed, session_factory


def test_one_message_per_run_for_many_targets(pages, db_session):
    src, ts, feed, sf = pages
    rec = Rec()
    pipeline.run_source(sf, "custom-p", notifier=rec, from_raw=feed(["a", "b", "c"]))
    run = pipeline.run_source(sf, "custom-p", notifier=rec,
                              from_raw=feed(["a2\nx", "b2", "c"]))
    assert run.status == "ok" and len(rec.calls) == 2
    extras = rec.calls[-1][1]
    assert extras["page_kpis"] == ["A", "B"]
    assert extras["excerpt"] == {"added": ["a2", "x"], "removed": ["a"]}
    assert extras["api_counts"] == (0, 0, 0)


def test_page_change_excerpt_in_changes_api(pages, db_session, session_factory, monkeypatch):
    src, ts, feed, sf = pages
    pipeline.run_source(sf, "custom-p", from_raw=feed(["a", "b", "c"]))
    run = pipeline.run_source(sf, "custom-p", from_raw=feed(["a2", "b", "c"]))
    monkeypatch.setattr(get_settings(), "dashboard_password", "secret")
    app = create_app()
    app.dependency_overrides[deps.get_session] = lambda: db_session
    client = TestClient(app)
    client.auth = ("u", "secret")
    items = client.get(f"/api/runs/{run.id}/changes").json()["items"]
    assert items[0]["dataset"] == "page"
    assert items[0]["excerpt"] == {"added": ["a2"], "removed": ["a"]}
    # non-page rows carry no excerpt key
    from app.models import Change
    db_session.add(Change(run_id=run.id, source_id=src.id, key_hash="0" * 40,
                          change_type="added", dataset="api", indicator="Z", country="",
                          period=""))
    db_session.commit()
    items = client.get(f"/api/runs/{run.id}/changes").json()["items"]
    assert "excerpt" not in [i for i in items if i["dataset"] == "api"][0]
