"""Set-based diff of staged canonical rows against data_points.

Rows of one source are bulk-loaded into a temp table, then compared and applied
with single SQL statements, never per-row Python loops.

stage_rows, compute_diff and apply_diff must share one transaction: staging is
ON COMMIT DROP, so a commit between them loses the staged rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.canonical import CanonicalRow

_COLUMNS = (
    "dataset, indicator, country, period, dims, key_hash, value_num, value_text, value_hash"
)


class DuplicateKeyError(Exception):
    """Same key appears with different values in one fetch."""


@dataclass
class DiffResult:
    existing: int
    total: int
    added: int
    altered: int
    removed: int


def stage_rows(session: Session, rows: Iterable[CanonicalRow]) -> int:
    """Load rows into TEMP TABLE staging; returns the distinct row count."""
    session.execute(text("DROP TABLE IF EXISTS staging"))
    session.execute(
        text(
            """
            CREATE TEMP TABLE staging (
                dataset text NOT NULL,
                indicator text NOT NULL,
                country text NOT NULL,
                period text NOT NULL,
                dims jsonb NOT NULL,
                key_hash char(40) NOT NULL,
                value_num numeric,
                value_text text,
                value_hash char(40) NOT NULL
            ) ON COMMIT DROP
            """
        )
    )
    raw = session.connection().connection
    with raw.cursor() as cur:
        with cur.copy(f"COPY staging ({_COLUMNS}) FROM STDIN") as copy:
            for r in rows:
                copy.write_row(
                    (
                        r.dataset, r.indicator, r.country, r.period, r.dims_json,
                        r.key_hash, r.value_num, r.value_text, r.value_hash,
                    )
                )

    conflicts = session.execute(
        text(
            """
            SELECT dataset, indicator, country, period, dims::text
            FROM staging GROUP BY dataset, indicator, country, period, dims, key_hash
            HAVING count(DISTINCT value_hash) > 1
            ORDER BY dataset, indicator, country, period
            LIMIT 5
            """
        )
    ).all()
    if conflicts:
        keys = ", ".join(f"{d}/{i}/{c}/{p} {dims}" for d, i, c, p, dims in conflicts)
        raise DuplicateKeyError(f"conflicting duplicate keys: {keys}")

    # Remaining duplicates are identical: keep one physical row per key.
    session.execute(
        text(
            "DELETE FROM staging a USING staging b "
            "WHERE a.key_hash = b.key_hash AND a.ctid > b.ctid"
        )
    )
    session.execute(text("CREATE UNIQUE INDEX ON staging (key_hash)"))
    session.execute(text("ANALYZE staging"))
    return session.execute(text("SELECT count(*) FROM staging")).scalar_one()


def compute_diff(session: Session, source_id: int, complete: bool,
                 exclude_indicators: Sequence[str] = ()) -> DiffResult:
    """Counts staging vs data_points; rows of `exclude_indicators` are not counted."""
    row = session.execute(
        text(
            """
            SELECT
              (SELECT count(*) FROM data_points WHERE source_id = :sid
                  AND NOT (indicator = ANY(CAST(:excl AS text[])))) AS existing,
              (SELECT count(*) FROM staging) AS total,
              (SELECT count(*) FROM staging s WHERE NOT EXISTS (
                  SELECT 1 FROM data_points d
                  WHERE d.source_id = :sid AND d.key_hash = s.key_hash)
                  AND NOT (s.indicator = ANY(CAST(:excl AS text[])))) AS added,
              (SELECT count(*) FROM staging s JOIN data_points d
                  ON d.source_id = :sid AND d.key_hash = s.key_hash
                  WHERE d.value_hash <> s.value_hash
                    AND NOT (s.indicator = ANY(CAST(:excl AS text[])))) AS altered,
              (SELECT CASE WHEN :complete THEN count(*) ELSE 0 END
                  FROM data_points d WHERE d.source_id = :sid AND NOT EXISTS (
                  SELECT 1 FROM staging s WHERE s.key_hash = d.key_hash)
                  AND NOT (d.indicator = ANY(CAST(:excl AS text[])))) AS removed
            """
        ),
        {"sid": source_id, "complete": complete, "excl": list(exclude_indicators)},
    ).one()
    return DiffResult(*row)


def _insert_changes(table: str, session: Session, source_id: int, run_id: int, complete: bool,
                    exclude_indicators: Sequence[str] = ()) -> None:
    """Insert the would-be changes (staging vs data_points) into `table`; never touches data_points."""
    removed_clause = "OR s.key_hash IS NULL" if complete else ""
    session.execute(
        text(
            f"""
            INSERT INTO {table} (run_id, source_id, key_hash, change_type, dataset,
                indicator, country, period, dims, old_num, old_text, new_num, new_text)
            SELECT :rid, :sid, COALESCE(s.key_hash, d.key_hash),
                CASE WHEN d.key_hash IS NULL THEN 'added'
                     WHEN s.key_hash IS NULL THEN 'removed' ELSE 'altered' END,
                COALESCE(s.dataset, d.dataset), COALESCE(s.indicator, d.indicator),
                COALESCE(s.country, d.country), COALESCE(s.period, d.period),
                COALESCE(s.dims, d.dims),
                d.value_num, d.value_text, s.value_num, s.value_text
            FROM staging s
            FULL OUTER JOIN (SELECT * FROM data_points WHERE source_id = :sid) d
                ON d.key_hash = s.key_hash
            WHERE (d.key_hash IS NULL OR s.value_hash <> d.value_hash {removed_clause})
              AND NOT (COALESCE(s.indicator, d.indicator) = ANY(CAST(:excl AS text[])))
            """
        ),
        {"sid": source_id, "rid": run_id, "excl": list(exclude_indicators)},
    )


def record_pending(session: Session, source_id: int, run_id: int, complete: bool,
                   exclude_indicators: Sequence[str] = ()) -> None:
    """Store a held run's would-be changes in pending_changes (data_points untouched)."""
    _insert_changes("pending_changes", session, source_id, run_id, complete, exclude_indicators)


def apply_diff(
    session: Session, source_id: int, run_id: int, complete: bool, record_changes: bool,
    exclude_indicators: Sequence[str] = (),
) -> None:
    """Apply staging to data_points. Rows of `exclude_indicators` are still upserted but
    never produce changes rows."""
    params = {"sid": source_id, "rid": run_id}
    # Order matters: changes read old values, so they go in before data_points is touched.
    if record_changes:
        _insert_changes("changes", session, source_id, run_id, complete, exclude_indicators)
    session.execute(
        text(
            """
            UPDATE data_points d SET value_num = s.value_num, value_text = s.value_text,
                value_hash = s.value_hash, last_changed_at = now()
            FROM staging s
            WHERE d.source_id = :sid AND d.key_hash = s.key_hash
              AND d.value_hash <> s.value_hash
            """
        ),
        params,
    )
    session.execute(
        text(
            """
            INSERT INTO data_points (source_id, key_hash, dataset, indicator, country,
                period, dims, value_num, value_text, value_hash)
            SELECT :sid, s.key_hash, s.dataset, s.indicator, s.country, s.period, s.dims,
                s.value_num, s.value_text, s.value_hash
            FROM staging s
            WHERE NOT EXISTS (SELECT 1 FROM data_points d
                              WHERE d.source_id = :sid AND d.key_hash = s.key_hash)
            """
        ),
        params,
    )
    if complete:
        # Excluded (baseline) indicators are deleted too when absent from the fetch: they are
        # intentionally not counted or recorded as changes.
        session.execute(
            text(
                """
                DELETE FROM data_points d WHERE d.source_id = :sid AND NOT EXISTS (
                    SELECT 1 FROM staging s WHERE s.key_hash = d.key_hash)
                """
            ),
            params,
        )
