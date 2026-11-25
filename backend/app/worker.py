"""Scheduler worker: cron jobs per source, queued-run poller, job reloader."""

from __future__ import annotations

import logging
import signal
import threading
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select, update

from app.connectors.registry import CONNECTORS, sync_sources
from app.core.pipeline import (
    Notifier, RunNotQueuedError, SessionFactory, SourceBusyError, _acquire_source_lock,
    _safe_error, run_source,
)
from app.models import Run, Source

log = logging.getLogger(__name__)

POLL_SECONDS = 15
RELOAD_SECONDS = 60
STALE_ERROR = "worker restarted while running"


def _job_id(slug: str) -> str:
    return f"source:{slug}"


def _scheduled_run(session_factory: SessionFactory, slug: str, notifier: Notifier | None) -> None:
    try:
        run_source(session_factory, slug, trigger="schedule", notifier=notifier)
    except SourceBusyError:
        log.warning("skipped: source busy (%s)", slug)
    except Exception:  # noqa: BLE001 - keep the scheduler thread alive
        log.exception("scheduled run of %s crashed", slug)


def reload_jobs(scheduler: BackgroundScheduler, session_factory: SessionFactory,
                notifier: Notifier | None = None) -> None:
    """Make source:* jobs match the DB: add, remove, reschedule when cron changed."""
    session = session_factory()
    try:
        sources = session.scalars(select(Source).where(Source.enabled.is_(True))).all()
        wanted: dict[str, str] = {}
        for s in sources:
            if s.slug not in CONNECTORS:
                log.warning("source %s has no registered connector; skipping", s.slug)
                continue
            wanted[s.slug] = s.cron
    finally:
        session.close()

    existing = {j.id: j for j in scheduler.get_jobs() if j.id.startswith("source:")}
    for job_id in set(existing) - {_job_id(slug) for slug in wanted}:
        scheduler.remove_job(job_id)
    for slug, cron in wanted.items():
        try:
            trigger = CronTrigger.from_crontab(cron, timezone="UTC")
        except ValueError:
            log.warning("source %s has invalid cron %r; skipping", slug, cron)
            continue
        job = existing.get(_job_id(slug))
        if job is None:
            scheduler.add_job(
                _scheduled_run, trigger, args=[session_factory, slug, notifier],
                id=_job_id(slug), max_instances=1, coalesce=True, misfire_grace_time=3600,
                replace_existing=True,
            )
        elif str(job.trigger) != str(trigger):
            scheduler.reschedule_job(job.id, trigger=trigger)


def poll_queued_once(session_factory: SessionFactory, notifier: Notifier | None) -> int:
    """Execute every queued run (oldest first); returns how many were started."""
    session = session_factory()
    try:
        queued = session.execute(
            select(Run.id, Run.trigger, Source.slug)
            .join(Source, Source.id == Run.source_id)
            .where(Run.status == "queued")
            .order_by(Run.id)
        ).all()
    finally:
        session.close()
    started = 0
    for run_id, trigger, slug in queued:
        try:
            run_source(session_factory, slug, trigger=trigger, notifier=notifier, run_id=run_id)
            started += 1
        except SourceBusyError:
            log.info("source %s busy; run %s stays queued", slug, run_id)
        except RunNotQueuedError:
            log.info("run %s already claimed; skipping", run_id)
        except Exception as e:  # noqa: BLE001
            log.exception("queued run %s (%s) crashed", run_id, slug)
            try:
                _mark_failed(session_factory, run_id, _safe_error(e))
            except Exception:  # noqa: BLE001
                log.exception("could not mark run %s failed", run_id)
    return started


def _mark_failed(session_factory: SessionFactory, run_id: int, error: str) -> None:
    session = session_factory()
    try:
        session.execute(
            update(Run).where(Run.id == run_id, Run.status.in_(("queued", "running")))
            .values(status="failed", error=error, finished_at=datetime.now(timezone.utc))
        )
        session.commit()
    finally:
        session.close()


def sweep_stale_runs(session_factory: SessionFactory) -> int:
    """Mark orphaned `running` runs failed, unless a live process holds the source lock."""
    session = session_factory()
    try:
        rows = session.execute(
            select(Run.id, Run.source_id).where(Run.status == "running").order_by(Run.id)
        ).all()
        session.rollback()
        swept = 0
        for run_id, source_id in rows:
            try:
                release = _acquire_source_lock(session, source_id)
            except SourceBusyError:
                log.info("run %s not swept: source %s is being run elsewhere", run_id, source_id)
                continue
            try:
                res = session.execute(
                    update(Run).where(Run.id == run_id, Run.status == "running")
                    .values(status="failed", error=STALE_ERROR,
                            finished_at=datetime.now(timezone.utc))
                )
                session.commit()
                swept += res.rowcount
            finally:
                release()
        return swept
    finally:
        session.close()


def build_scheduler(session_factory: SessionFactory, notifier: Notifier | None) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(
        timezone="UTC",
        job_defaults={"misfire_grace_time": 3600, "coalesce": True, "max_instances": 1},
    )
    reload_jobs(scheduler, session_factory, notifier)
    scheduler.add_job(
        poll_queued_once, "interval", seconds=POLL_SECONDS, args=[session_factory, notifier],
        id="poll-queued", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        reload_jobs, "interval", seconds=RELOAD_SECONDS, args=[scheduler, session_factory, notifier],
        id="reload-jobs", max_instances=1, coalesce=True,
    )
    return scheduler


def main() -> None:
    from app.db import SessionLocal
    from app.notify.mattermost import notifier_from_settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("httpx", "httpcore"):  # their INFO lines print request URLs (webhook secret)
        logging.getLogger(noisy).setLevel(logging.WARNING)
    with SessionLocal() as session:
        sync_sources(session)
    swept = sweep_stale_runs(SessionLocal)
    if swept:
        log.warning("marked %d stale running run(s) as failed", swept)
    scheduler = build_scheduler(SessionLocal, notifier_from_settings())
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    scheduler.start()
    log.info("worker started with %d source job(s)",
             len([j for j in scheduler.get_jobs() if j.id.startswith("source:")]))
    while not stop.wait(1):
        pass
    log.info("shutting down")
    scheduler.shutdown(wait=True)


if __name__ == "__main__":
    main()
