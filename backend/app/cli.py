"""CLI: python -m app.cli sync-sources | run <slug> [--from-raw PATH]."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from app.connectors.registry import CONNECTORS, sync_sources
from app.core.pipeline import SourceBusyError, run_source
from app.db import SessionLocal
from app.notify.mattermost import notifier_from_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync-sources", help="upsert one sources row per connector")
    sub.add_parser("backfill-rollup", help="rebuild change_rollup from all changes")
    run_p = sub.add_parser("run", help="run one source now")
    run_p.add_argument("slug")
    run_p.add_argument("--from-raw", type=Path, help="use this file instead of downloading")
    args = parser.parse_args(argv)
    for noisy in ("httpx", "httpcore"):  # their INFO lines print request URLs (webhook secret)
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.cmd == "sync-sources":
        with SessionLocal() as session:
            sync_sources(session)
        print(f"synced {len(CONNECTORS)} sources")
        return 0

    if args.cmd == "backfill-rollup":
        from app.core.rollup import backfill

        with SessionLocal() as session:
            n = backfill(session)
        print(f"rebuilt change_rollup: {n} rows")
        return 0

    if args.slug not in CONNECTORS:
        print(f"unknown source: {args.slug}", file=sys.stderr)
        return 2
    if args.from_raw is not None and not args.from_raw.is_file():
        print(f"raw file not found: {args.from_raw}", file=sys.stderr)
        return 2
    try:
        run = run_source(SessionLocal, args.slug, trigger="manual", notifier=notifier_from_settings(),
                         from_raw=args.from_raw)
    except SourceBusyError:
        print(f"{args.slug}: busy")
        return 3
    print(f"{args.slug}: {run.status} rows={run.rows_total} added={run.added} "
          f"altered={run.altered} removed={run.removed}")
    return 1 if run.status == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
