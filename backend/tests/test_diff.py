import time
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.canonical import RawRow, to_canonical
from app.core.diff import DiffResult, DuplicateKeyError, apply_diff, compute_diff, stage_rows
from app.models import Change, DataPoint
from tests.conftest import row, seed


def test_added_altered_removed(db_session, source):
    seed(db_session, source, [row("FRA", "2023", 1), row("FRA", "2024", 2), row("DEU", "2024", 3)])
    stage_rows(db_session, [row("FRA", "2023", 1), row("FRA", "2024", 2.5), row("ITA", "2024", 4)])
    assert compute_diff(db_session, source.id, complete=True) == DiffResult(
        existing=3, total=3, added=1, altered=1, removed=1
    )


def test_incomplete_fetch_never_removes(db_session, source):
    seed(db_session, source, [row("FRA", "2023", 1), row("DEU", "2024", 3)])
    stage_rows(db_session, [row("FRA", "2023", 1)])
    assert compute_diff(db_session, source.id, complete=False).removed == 0


def test_apply_writes_changes_with_old_and_new(db_session, source, run):
    seed(db_session, source, [row("FRA", "2024", 2)])
    stage_rows(db_session, [row("FRA", "2024", 2.5)])
    apply_diff(db_session, source.id, run.id, complete=True, record_changes=True)
    ch = db_session.scalars(select(Change)).one()
    assert (ch.change_type, ch.old_num, ch.new_num) == ("altered", Decimal("2"), Decimal("2.5"))


def test_apply_added_removed_and_data_points(db_session, source, run):
    seed(db_session, source, [row("FRA", "2023", 1), row("DEU", "2024", 3)])
    before = {d.country: d.last_changed_at for d in db_session.scalars(select(DataPoint))}
    stage_rows(db_session, [row("FRA", "2023", 1), row("ITA", "2024", 4)])
    apply_diff(db_session, source.id, run.id, complete=True, record_changes=True)
    changes = {c.country: c for c in db_session.scalars(select(Change))}
    assert set(changes) == {"ITA", "DEU"}
    assert changes["ITA"].change_type == "added" and changes["ITA"].new_num == Decimal("4")
    assert changes["DEU"].change_type == "removed" and changes["DEU"].old_num == Decimal("3")
    db_session.expire_all()
    dps = {d.country: d for d in db_session.scalars(select(DataPoint))}
    assert set(dps) == {"FRA", "ITA"}
    assert dps["FRA"].last_changed_at == before["FRA"]


def test_apply_incomplete_keeps_missing(db_session, source, run):
    seed(db_session, source, [row("FRA", "2023", 1), row("DEU", "2024", 3)])
    stage_rows(db_session, [row("FRA", "2023", 1)])
    apply_diff(db_session, source.id, run.id, complete=False, record_changes=True)
    assert len(db_session.scalars(select(DataPoint)).all()) == 2
    assert db_session.scalars(select(Change)).all() == []


def test_identical_duplicates_collapse(db_session, source):
    assert stage_rows(db_session, [row("FRA", "2024", 2), row("FRA", "2024", "2.0")]) == 1


def test_conflicting_duplicates_raise(db_session, source):
    with pytest.raises(DuplicateKeyError, match="FRA/2024"):
        stage_rows(db_session, [row("FRA", "2024", 2), row("FRA", "2024", 3)])


def test_value_becoming_missing_is_removed_not_altered(db_session, source):
    seed(db_session, source, [row("FRA", "2024", 2)])
    stage_rows(db_session, [r for r in [to_canonical(RawRow("d", "i", "FRA", "2024", ".."))] if r])
    assert compute_diff(db_session, source.id, complete=True).removed == 1


@pytest.mark.slow
def test_200k_rows_under_30s(db_session, source, run):
    rows = [row(f"C{i % 200}", str(1900 + i // 200), i) for i in range(200_000)]
    seed(db_session, source, rows)
    changed = [row(f"C{i % 200}", str(1900 + i // 200), i if i % 10 else i + 1) for i in range(200_000)]
    t0 = time.monotonic()
    stage_rows(db_session, changed)
    res = compute_diff(db_session, source.id, complete=True)
    apply_diff(db_session, source.id, run.id, complete=True, record_changes=True)
    elapsed = time.monotonic() - t0
    print(f"200k stage+diff+apply: {elapsed:.2f}s")
    assert res.altered == 20_000 and res.added == 0 and res.removed == 0
    assert elapsed < 30
