from sqlalchemy import inspect


def test_upgrade_creates_tables(db_engine):
    insp = inspect(db_engine)
    tables = set(insp.get_table_names())
    assert {"sources", "indicators", "data_points", "runs", "changes", "uploads"} <= tables
    uniques = insp.get_unique_constraints("data_points")
    assert any(u["column_names"] == ["source_id", "key_hash"] for u in uniques)


def test_db_session_rolls_back_even_after_commit(db_engine, db_session):
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from app.models import Source

    db_session.add(Source(slug="x", name="X", area="a", access="api"))
    db_session.commit()
    assert db_session.scalar(select(func.count()).select_from(Source)) == 1
    with Session(db_engine) as other:
        assert other.scalar(select(func.count()).select_from(Source)) == 0
