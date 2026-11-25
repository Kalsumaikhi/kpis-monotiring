from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.main import create_app
from app.config import get_settings
from app.connectors.registry import CONNECTORS
from app.core.canonical import RawRow
from app.models import PendingChange, Run, Source, Upload
from app.worker import poll_queued_once
from tests.conftest import FakeConnector


@pytest.fixture
def client(db_session, session_factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "dashboard_password", "secret")
    app = create_app()
    app.dependency_overrides[deps.get_session] = lambda: db_session
    app.dependency_overrides[deps.get_session_factory] = lambda: session_factory
    c = TestClient(app)
    c.auth = ("u", "secret")
    return c


@pytest.fixture
def manual(db_session, monkeypatch):
    c = FakeConnector(rows=[RawRow("d", "i", "DEU", "2020", 1.0)])
    c.slug = "up"
    c.download = lambda client, dest: (_ for _ in ()).throw(AssertionError("no download"))
    monkeypatch.setitem(CONNECTORS, "up", c)
    s = Source(slug="up", name="Up", area="a", access="manual")
    db_session.add(s)
    db_session.commit()
    return s


def post(client, slug="up", data=b"hello", who="ana"):
    return client.post("/api/uploads", data={"source": slug, "who": who},
                       files={"file": ("f.json", data)})


def test_upload_rejects_non_manual_source(client, db_session):
    db_session.add(Source(slug="w", name="W", area="a", access="api"))
    db_session.commit()
    assert post(client, "w").status_code == 422
    assert post(client, "nope").status_code in (404, 422)


def test_upload_too_large(client, manual, monkeypatch):
    import app.api.routes_uploads as ru
    monkeypatch.setattr(ru, "MAX_UPLOAD_BYTES", 10)
    assert post(client, data=b"x" * 11).status_code == 413


def test_upload_creates_queued_held_run(client, manual, db_session, session_factory):
    # baseline first, so the upload has history and is held rather than baselined
    db_session.add(Run(source_id=manual.id, status="ok", trigger="manual"))
    from app.core.diff import apply_diff, stage_rows
    from app.core.canonical import to_canonical
    stage_rows(db_session, [to_canonical(RawRow("d", "i", "FRA", "2020", 5.0))])
    apply_diff(db_session, manual.id, 0, complete=True, record_changes=False)
    db_session.commit()

    r = post(client)
    assert r.status_code == 202
    rid = r.json()["run_id"]
    run = db_session.get(Run, rid)
    assert run.status == "queued" and run.trigger == "upload"
    assert Path(run.raw_path).read_bytes() == b"hello"
    up = db_session.query(Upload).one()
    assert (up.who, up.filename, up.run_id) == ("ana", "f.json", rid)
    assert poll_queued_once(session_factory, None) == 1
    db_session.expire_all()
    run = db_session.get(Run, rid)
    assert run.status == "suspect_pending"
    assert db_session.query(PendingChange).filter_by(run_id=rid).count() > 0
    lst = client.get("/api/uploads").json()
    assert lst["items"][0]["run_status"] == "suspect_pending"
    assert client.get("/api/sources?access=manual").json()[0]["slug"] == "up"
    assert client.get("/api/sources?access=watch").json() == []


def test_upload_409_when_a_check_is_already_queued(client, manual, db_session, session_factory):
    from app.config import get_settings as gs
    db_session.add(Run(source_id=manual.id, status="queued", trigger="manual"))
    db_session.commit()
    r = post(client)
    assert r.status_code == 409
    assert r.json() == {"detail": "a check for this source is already queued"}
    raw = Path(gs().raw_dir)
    assert [p for p in raw.rglob("*") if p.is_file()] == []  # the saved file was removed
    assert db_session.query(Upload).count() == 0
    assert db_session.query(Run).filter(Run.source_id == manual.id).count() == 1
