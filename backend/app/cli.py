"""CLI: python -m app.cli sync-sources | run <slug> [--from-raw PATH]."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from app.connectors.registry import CONNECTORS, sync_sources
from app.core.pipeline import SourceBusyError, run_source
from app.db import SessionLocal
from app.notify.mattermost import notifier_from_settings


class _FromRaw:
    """Connector wrapper whose download() copies a local file instead of fetching."""

    def __init__(self, inner, path: Path):
        self._inner = inner
        self._path = path

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def download(self, client, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._path, dest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync-sources", help="upsert one sources row per connector")
    run_p = sub.add_parser("run", help="run one source now")
    run_p.add_argument("slug")
    run_p.add_argument("--from-raw", type=Path, help="use this file instead of downloading")
    args = parser.parse_args(argv)

    if args.cmd == "sync-sources":
        with SessionLocal() as session:
            sync_sources(session)
        print(f"synced {len(CONNECTORS)} sources")
        return 0

    if args.slug not in CONNECTORS:
        print(f"unknown source: {args.slug}", file=sys.stderr)
        return 2
    original = CONNECTORS[args.slug]
    if args.from_raw is not None:
        if not args.from_raw.is_file():
            print(f"raw file not found: {args.from_raw}", file=sys.stderr)
            return 2
        CONNECTORS[args.slug] = _FromRaw(original, args.from_raw)
    try:
        run = run_source(SessionLocal, args.slug, trigger="manual", notifier=notifier_from_settings())
    except SourceBusyError:
        print(f"{args.slug}: busy")
        return 3
    finally:
        CONNECTORS[args.slug] = original
    print(f"{args.slug}: {run.status} rows={run.rows_total} added={run.added} "
          f"altered={run.altered} removed={run.removed}")
    return 1 if run.status == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
