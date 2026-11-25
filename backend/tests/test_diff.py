import time
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from app.core.canonical import RawRow, to_canonical
from app.core.diff import DiffResult, DuplicateKeyError, apply_diff, compute_diff, stage_rows
from app.models import Change, DataPoint, Source
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
    seed(db_session, source, [row("FRA", "2023", 1), row("FRA", "2024", 2), row("DEU", "2024", 3)])
    past = datetime(2000, 1, 1, tzinfo=timezone.utc)
    db_session.execute(update(DataPoint).values(last_changed_at=past))
    stage_rows(db_session, [row("FRA", "2023", 1), row("FRA", "2024", 2.5), row("ITA", "2024", 4)])
    apply_diff(db_session, source.id, run.id, complete=True, record_changes=True)
    changes = {c.country: c for c in db_session.scalars(select(Change))}
    assert {c.country for c in changes.values()} == {"ITA", "DEU", "FRA"}
    assert changes["ITA"].change_type == "added" and changes["ITA"].new_num == Decimal("4")
    assert changes["DEU"].change_type == "removed" and changes["DEU"].old_num == Decimal("3")
    db_session.expire_all()
    dps = {(d.country, d.period): d for d in db_session.scalars(select(DataPoint))}
    assert set(dps) == {("FRA", "2023"), ("FRA", "2024"), ("ITA", "2024")}
    assert dps[("FRA", "2023")].last_changed_at == past
    assert dps[("FRA", "2024")].last_changed_at > past
    assert dps[("ITA", "2024")].last_changed_at > past


def test_apply_does_not_touch_other_source(db_session, source, run):
    other = Source(slug="s2", name="S2", area="a", access="manual")
    db_session.add(other)
    db_session.flush()
    rows = [row("FRA", "2023", 1), row("FRA", "2024", 2), row("DEU", "2024", 3)]
    seed(db_session, source, rows)
    seed(db_session, other, rows)

    def snapshot():
        return sorted(
            (d.key_hash, d.value_hash, d.last_changed_at)
            for d in db_session.scalars(select(DataPoint).where(DataPoint.source_id == other.id))
        )

    before = snapshot()
    stage_rows(db_session, [row("FRA", "2023", 9), row("ITA", "2024", 4)])
    apply_diff(db_session, source.id, run.id, complete=True, record_changes=True)
    db_session.expire_all()
    assert snapshot() == before and len(before) == 3
    assert db_session.scalars(select(Change).where(Change.source_id == other.id)).all() == []
    assert len(db_session.scalars(select(Change).where(Change.source_id == source.id)).all()) == 4


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
    assert db_session.scalar(select(func.count()).select_from(Change)) == 20_000


def _ind(indicator, period, value):
    return to_canonical(RawRow("d", indicator, "", period, value))


def test_exclude_indicators_not_counted_but_applied(db_session, source, run):
    from app.models import PendingChange
    seed(db_session, source, [_ind("A", "1", 1), _ind("B", "1", 1), _ind("C", "1", 1)])
    stage_rows(db_session, [_ind("A", "1", 2), _ind("B", "1", 2), _ind("N", "1", 5)])
    d = compute_diff(db_session, source.id, complete=True, exclude_indicators=["B", "N", "C"])
    assert d == DiffResult(existing=1, total=3, added=0, altered=1, removed=0)
    apply_diff(db_session, source.id, run.id, complete=True, record_changes=True,
               exclude_indicators=["B", "N", "C"])
    assert [(c.indicator, c.change_type) for c in db_session.scalars(select(Change))] == [("A", "altered")]
    pts = {p.indicator: p.value_num for p in db_session.scalars(select(DataPoint))}
    assert pts == {"A": Decimal("2"), "B": Decimal("2"), "N": Decimal("5")}  # C absent -> deleted
    assert db_session.scalars(select(PendingChange)).all() == []


def test_record_pending_excludes_indicators(db_session, source, run):
    from app.core.diff import record_pending
    from app.models import PendingChange
    seed(db_session, source, [_ind("A", "1", 1), _ind("B", "1", 1)])
    stage_rows(db_session, [_ind("A", "1", 2), _ind("B", "1", 2), _ind("N", "1", 5)])
    record_pending(db_session, source.id, run.id, True, ["B", "N"])
    assert [(p.indicator, p.change_type) for p in db_session.scalars(select(PendingChange))] == [("A", "altered")]
