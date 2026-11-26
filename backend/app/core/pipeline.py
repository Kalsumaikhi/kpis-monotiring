"""Run orchestration: download, parse, diff, guard, apply, notify.

stage_rows, compute_diff and apply_diff share one transaction (staging is a
TEMP table ON COMMIT DROP), so nothing commits between them.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Protocol

import httpx
from sqlalchemy import Engine, case, delete, func, select, text, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app import credentials as creds
from app.connectors.base import IndicatorMeta
from app.connectors.registry import resolve_connector
from app.core import raw_store, rollup
from app.core.canonical import to_canonical
from app.core.diff import DiffResult, apply_diff, compute_diff, record_pending, stage_rows
from app.models import Change, DataPoint, PendingChange, Run, Source, WatchTarget

log = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]


class Notifier(Protocol):
    def notify_run(
        self, run: Run, source: Source, top_indicators: list[tuple[str, int]], streak: int,
        extras: dict | None = None,
    ) -> None: ...


class SourceBusyError(Exception):
    """Another run of this source holds the per-source advisory lock."""


class RunNotQueuedError(Exception):
    """The queued run was claimed elsewhere (or is not queued) before we could claim it."""


LOCK_NAMESPACE = 7421  # first key of pg_try_advisory_lock(int, int); second key is source_id


def _acquire_source_lock(session: Session, source_id: int) -> Callable[[], None]:
    """Session-level advisory lock on a dedicated connection; returns the release function.

    If the session is bound to an Engine, a fresh autocommit connection is used so the
    lock outlives the session's own transactions. If it is bound to a Connection (tests),
    that connection is reused.
    """
    bind = session.get_bind()
    own = isinstance(bind, Engine)
    conn = bind.connect().execution_options(isolation_level="AUTOCOMMIT") if own else bind
    params = {"ns": LOCK_NAMESPACE, "sid": source_id}
    try:
        got = conn.execute(text("SELECT pg_try_advisory_lock(:ns, :sid)"), params).scalar()
    except Exception:
        if own:
            conn.close()
        raise
    if not got:
        if own:
            conn.close()
        raise SourceBusyError(f"source {source_id} is busy")

    def release() -> None:
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:ns, :sid)"), params)
        except Exception:  # noqa: BLE001 - a dead connection has dropped the lock anyway
            log.exception("advisory unlock failed for source %s", source_id)
        finally:
            if own:
                conn.close()

    return release


_URL = re.compile(r"https?://\S+")


def _safe_error(e: Exception) -> str:
    """Error text for storage: URLs (which may embed secrets) redacted, truncated."""
    msg = _URL.sub("<url>", f"{type(e).__name__}: {e}")
    return msg[:2000]


def _safe_prune(session: Session, source_id: int) -> None:
    try:
        raw_store.prune(session, source_id, get_settings().raw_retention_runs)
    except Exception:  # noqa: BLE001 - housekeeping must not change the run outcome
        log.exception("raw prune failed for source %s", source_id)
        session.rollback()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_rows(rows: Iterable):
    for r in rows:
        c = to_canonical(r)
        if c is not None:
            yield c


def _upsert_indicators(session: Session, source_id: int, metas: list[IndicatorMeta]) -> None:
    # indicators.first_seen_at defaults to now() on insert and is never updated.
    for m in metas:
        session.execute(
            text(
                """
                INSERT INTO indicators (source_id, code, name, unit, description)
                VALUES (:sid, :code, :name, :unit, :description)
                ON CONFLICT (source_id, code) DO UPDATE
                SET name = EXCLUDED.name, unit = EXCLUDED.unit,
                    description = EXCLUDED.description
                """
            ),
            {"sid": source_id, "code": m.code, "name": m.name, "unit": m.unit,
             "description": m.description},
        )


def _streak(session: Session, run: Run) -> int:
    ids_statuses = session.execute(
        select(Run.status)
        .where(Run.source_id == run.source_id, Run.id <= run.id)
        .order_by(Run.id.desc())
    ).scalars()
    n = 0
    for status in ids_statuses:
        if status != "failed":
            break
        n += 1
    return n


def _top_indicators(session: Session, run: Run) -> list[tuple[str, int]]:
    if run.status != "ok":
        return []
    rows = session.execute(
        select(Change.indicator, func.count().label("n"))
        .where(Change.run_id == run.id)
        .group_by(Change.indicator)
        .order_by(func.count().desc(), Change.indicator)
        .limit(5)
    ).all()
    return [(i, n) for i, n in rows]


_NOT_FINISHED = ("queued", "running", "approve_queued", "superseded")
_INCOMPLETE = "incomplete:"


def _without_skip_marker(error: str | None) -> str:
    """run.error without a leading key-baseline skip marker (it is put first; see run_source)."""
    error = error or ""
    if error.startswith(SKIPPED_DELETIONS):
        _, _, error = error.partition("; ")
    return error


def _incomplete_note(session: Session, run: Run) -> str | None:
    """The run's warning when it became incomplete (the previous finished run was not)."""
    error = _without_skip_marker(run.error)
    if run.status not in ("ok", "no_change", "baseline") or not error.startswith(_INCOMPLETE):
        return None
    prev_error = session.execute(
        select(Run.error)
        .where(Run.source_id == run.source_id, Run.id < run.id, Run.status.not_in(_NOT_FINISHED))
        .order_by(Run.id.desc())
        .limit(1)
    ).first()
    if prev_error is not None and _without_skip_marker(prev_error[0]).startswith(_INCOMPLETE):
        return None
    return error[len(_INCOMPLETE):].strip()


def _notify_extras(session: Session, run: Run) -> dict:
    from app.connectors.watch.excerpt import page_excerpt

    extras: dict = {"page_kpis": [], "excerpt": None, "incomplete_note": _incomplete_note(session, run)}
    if run.status != "ok":
        return extras
    page = list(session.scalars(
        select(Change.indicator).where(Change.run_id == run.id, Change.dataset == "page")
        .distinct()
    ))
    order = {n: i for i, n in enumerate(session.scalars(  # target order, not alphabetical
        select(WatchTarget.kpi_name).where(WatchTarget.source_id == run.source_id)
        .order_by(WatchTarget.id)
    ))}
    page.sort(key=lambda n: (order.get(n, len(order)), n))
    if page:
        extras["page_kpis"] = page
        extras["excerpt"] = page_excerpt(session, run.source_id, page[0], run.id)
        counts = dict(session.execute(
            select(Change.change_type, func.count())
            .where(Change.run_id == run.id, Change.dataset != "page")
            .group_by(Change.change_type)
        ).all())
        extras["api_counts"] = (counts.get("added", 0), counts.get("altered", 0),
                                counts.get("removed", 0))
    return extras


def _notify(session: Session, run: Run, notifier, connector=None) -> None:
    """Send the run's message. `connector` (run_source only) may add extras through an
    optional notify_extras(session, run) hook, e.g. logins that now need a sign-in; they
    are taken whatever the run's final status."""
    hook = getattr(connector, "notify_extras", None)
    if notifier is None:
        return
    try:
        source = session.get(Source, run.source_id)
        extras = _notify_extras(session, run)
        if hook is not None:
            extras.update(hook(session, run))
        top = _top_indicators(session, run)
        if extras["page_kpis"]:
            top = [(i, n) for i, n in top if i not in extras["page_kpis"]]
        notifier.notify_run(run, source, top, _streak(session, run), extras=extras)
    except Exception as e:  # noqa: BLE001 - notifier problems must not fail the run
        log.error("notifier failed for run %s: %s", run.id, _safe_error(e))
        session.rollback()
        session.execute(
            update(Run).where(Run.id == run.id).values(notify_error=_safe_error(e))
        )
        session.commit()
        session.refresh(run)
        failed_hook = getattr(connector, "notify_failed", None)
        if failed_hook is not None:
            try:
                failed_hook(session, run)
            except Exception as e2:  # noqa: BLE001
                log.error("notify_failed hook failed for run %s: %s", run.id, _safe_error(e2))
                session.rollback()


def _detach(session: Session, run: Run) -> Run:
    """Load all attributes, then detach so the caller can read the run after close."""
    session.refresh(run)
    session.expunge(run)
    return run


def _fail(session: Session, run_id: int, e: Exception, cred=None, message: str | None = None) -> Run:
    session.rollback()
    run = session.get(Run, run_id)
    run.status = "failed"
    run.error = creds.redact(message if message is not None else _safe_error(e), cred)
    run.finished_at = _now()
    session.commit()
    return run


HELD_STATUSES = ("suspect_pending", "approve_queued")


def _supersede_held(session: Session, source_id: int, before_id: int | None = None,
                    except_id: int | None = None, reason: str = "a newer run") -> None:
    """Mark older held runs superseded and drop their pending changes."""
    q = update(Run).where(Run.source_id == source_id, Run.status.in_(HELD_STATUSES))
    if before_id is not None:
        q = q.where(Run.id < before_id)
    if except_id is not None:
        q = q.where(Run.id != except_id)
    # CASE reads the pre-update status, so the message says whether an approval was lost.
    msg = case(
        (Run.status == "approve_queued",
         f"superseded by {reason} before the approval was applied"),
        else_=f"superseded by {reason}",
    )
    ids = session.execute(
        q.values(status="superseded", error=msg).returning(Run.id)
    ).scalars().all()
    if ids:
        session.execute(delete(PendingChange).where(PendingChange.run_id.in_(ids)))


def _finish(session: Session, run: Run, status: str, diff: DiffResult, metas, vintage, warnings=None) -> None:
    run.status = status
    run.rows_total = diff.total
    run.added, run.altered, run.removed = diff.added, diff.altered, diff.removed
    run.vintage = vintage
    if warnings:
        run.error = "; ".join(warnings)[:2000]
    run.finished_at = _now()
    if status != "suspect_pending":
        # Metadata of an unapproved (held) file must not reach the indicators table.
        _upsert_indicators(session, run.source_id, metas)
        if status == "ok":
            rollup.refresh_rollup(session, run.id)  # same transaction as the applied changes
        # Newer good data makes any earlier held run stale: approving it would re-apply old removals.
        _supersede_held(session, run.source_id, before_id=run.id)
    session.commit()
    _safe_prune(session, run.source_id)


def _after_run(session: Session, connector, run: Run) -> None:
    """Optional connector hook for non-failed runs; its failure never fails the run."""
    hook = getattr(connector, "after_run", None)
    if hook is None:
        return
    try:
        hook(session, run)
    except Exception as e:  # noqa: BLE001
        log.error("after_run failed for run %s: %s", run.id, _safe_error(e))
        session.rollback()


def _on_failed(session: Session, connector, run: Run) -> None:
    """Optional connector hook for failed runs that have a raw file; never raises."""
    hook = getattr(connector, "on_failed", None)
    if hook is None:
        return
    try:
        hook(session, run)
    except Exception as e:  # noqa: BLE001
        log.error("on_failed failed for run %s: %s", run.id, _safe_error(e))
        session.rollback()


class _KeyFailure(Exception):
    """A key problem; str(e) is the run error, stored as is."""


class _KeyState:
    """Per-run key bookkeeping shared with the httpx hooks."""

    def __init__(self, cred: creds.Credential, hosts: tuple[str, ...]):
        self.cred = cred
        self.hosts = {h.lower() for h in hosts}
        self.rejected: int | None = None
        self.sent = False

    def on_request(self, request: httpx.Request) -> None:
        # Called for every hop. httpx copies earlier headers onto redirect requests, so an
        # ineligible hop (other host, or http) must have our key removed, not just not added.
        request.extensions = {**request.extensions, "kpi_keyed": False}
        url, headers = creds.apply(self.cred, str(request.url), {})
        if request.url.scheme == "https" and request.url.host.lower() in self.hosts:
            if url != str(request.url):
                request.url = httpx.URL(url)
            for k, v in headers.items():
                request.headers[k] = v
            request.extensions["kpi_keyed"] = True
            self.sent = True
            return
        for k, v in headers.items():  # strip only what carries our secret
            if request.headers.get(k) == v:
                del request.headers[k]
        stripped = creds.strip_query_secret(self.cred, str(request.url))
        if stripped != str(request.url):
            request.url = httpx.URL(stripped)

    def on_response(self, response: httpx.Response) -> None:
        if response.request.extensions.get("kpi_keyed") and response.status_code in (401, 403) \
                and self.rejected is None:
            self.rejected = response.status_code


def _key_hosts(connector) -> tuple[str, ...]:
    return tuple(getattr(connector, "credential_hosts", ()) or ())


def _load_key(session: Session, credential_id: int) -> creds.Credential:
    try:
        cred = creds.load(session, credential_id)
    except creds.KeyUnavailable as e:
        raise _KeyFailure(f"key unavailable ({e})") from None
    except KeyError:
        raise _KeyFailure("key unavailable (key not found)") from None
    if cred.kind == "login":  # never sent as a header/query value (it is a password)
        raise _KeyFailure("login keys are only used by KPIs")
    return cred


def _record_key_use(session: Session, credential_id: int | None, error: str | None) -> None:
    if credential_id is None:
        return
    try:
        creds.mark_used(session, credential_id, error)
    except Exception:  # noqa: BLE001 - bookkeeping must not change the run outcome
        log.warning("could not record use of key %s", credential_id)


def _scrub_raw(dest: Path, cred: creds.Credential) -> None:
    """Redact the secret from the raw file (and a leftover `.part`), if present."""
    for path in (dest, dest.with_name(dest.name + ".part")):
        try:
            if path.exists():
                data = path.read_bytes()
                clean = creds.redact_bytes(data, cred)
                if clean != data:
                    path.write_bytes(clean)
        except OSError:
            log.warning("could not scrub raw file %s; deleting it", path.name)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.error("could not delete unscrubbed raw file %s", path.name)


SKIPPED_DELETIONS = "key baseline skipped deletions"  # also read by the notifier


def _skipped_deletions_before(session: Session, source_id: int, run_id: int) -> bool:
    """Whether the source's previous applied run was a key baseline that skipped deletions
    under the current key: runs started before the flag was last set (an earlier key) don't
    count. Failed and held runs don't count, so a failure in between doesn't restart it."""
    q = (select(Run.error)
         .where(Run.source_id == source_id, Run.id < run_id,
                Run.status.in_(("ok", "no_change", "baseline"))))
    since = session.scalar(select(Source.needs_baseline_at).where(Source.id == source_id))
    if since is not None:
        q = q.where(Run.started_at >= since)
    error = session.scalar(q.order_by(Run.id.desc()).limit(1))
    return SKIPPED_DELETIONS in (error or "")


def run_source(
    session_factory: SessionFactory,
    slug: str,
    trigger: str = "schedule",
    notifier: Notifier | None = None,
    run_id: int | None = None,
    force_hold: bool = False,
    from_raw: str | Path | None = None,
) -> Run:
    """Run one source. With run_id, reuse that queued Run row (keeping its trigger).

    from_raw: copy this file to the run's raw path instead of downloading.

    force_hold holds any run that has changes as suspect_pending (never a baseline).
    """
    settings = get_settings()
    session = session_factory()
    release = None
    try:
        connector = resolve_connector(session, slug)  # KeyError for unknown slug, before any row
        source = session.scalars(select(Source).where(Source.slug == slug)).one()
        release = _acquire_source_lock(session, source.id)  # SourceBusyError before any row
        if run_id is not None:
            claimed = session.execute(
                update(Run)
                .where(Run.id == run_id, Run.source_id == source.id, Run.status == "queued")
                .values(status="running", started_at=func.clock_timestamp())
            ).rowcount
            session.commit()
            if claimed == 0:
                raise RunNotQueuedError(f"run {run_id} is not a queued run of {slug}")
            run = session.get(Run, run_id)
        else:
            # The real start time (not the transaction's): compared with needs_baseline_at.
            run = Run(source_id=source.id, status="running", trigger=trigger,
                      started_at=func.clock_timestamp())
            session.add(run)
            session.commit()
        run_id = run.id
        # Read under the lock: a key change takes it too, so this stays true for the whole
        # run (see patch_source). Only a fetched run is the new key's baseline.
        session.refresh(source)
        key_baseline = source.needs_baseline and from_raw is None and not force_hold

        cred = None
        state = None
        key_id = source.credential_id if _key_hosts(connector) and from_raw is None else None
        key_error: str | None = None
        try:
            suffix = connector.raw_suffix
            dest = raw_store.path_for(slug, run_id, suffix)
            run.raw_path = str(dest)
            session.commit()
            if from_raw is not None:
                if Path(from_raw).resolve() != dest.resolve():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(from_raw, dest)
            else:
                hooks: dict = {}
                if key_id is not None:
                    cred = _load_key(session, key_id)
                    # End the read transaction now: the session must not sit idle in a
                    # transaction during a long download (nothing below relies on it).
                    session.commit()
                    state = _KeyState(cred, _key_hosts(connector))
                    hooks = {"request": [state.on_request], "response": [state.on_response]}
                try:
                    with httpx.Client(timeout=60, follow_redirects=True, event_hooks=hooks) as client:
                        connector.download(client, dest)
                except Exception:
                    if state is not None and state.rejected is not None:
                        raise _KeyFailure(f"key rejected (HTTP {state.rejected})") from None
                    raise
                finally:  # the raw file must not hold the secret, whether or not download failed
                    if cred is not None:
                        _scrub_raw(dest, cred)
                if state is not None and state.rejected is not None:
                    raise _KeyFailure(f"key rejected (HTTP {state.rejected})")
        except Exception as e:  # noqa: BLE001
            if isinstance(e, _KeyFailure):
                key_error = str(e)
            run = _fail(session, run_id, e, cred, message=key_error)
            if key_error is not None:  # an unrelated failure says nothing about the key
                _record_key_use(session, key_id, key_error)
            _on_failed(session, connector, run)
            _notify(session, run, notifier, connector)
            return _detach(session, run)

        if key_id is not None and cred is not None and state is not None and state.sent:
            _record_key_use(session, key_id, None)

        try:
            parsed = connector.parse(dest)
            excl = parsed.baseline_indicators
            stage_rows(session, _canonical_rows(parsed.rows))
            diff = compute_diff(session, source.id, parsed.complete, excl)
            has_history = session.scalar(
                select(
                    select(DataPoint.id).where(DataPoint.source_id == source.id).exists()
                    | select(Run.id)
                    .where(Run.source_id == source.id, Run.id < run_id,
                           Run.status.in_(("baseline", "ok")))
                    .exists()
                )
            )
            run = session.get(Run, run_id)
            warnings = [creds.redact(w, cred) for w in parsed.warnings]
            if not has_history or key_baseline:
                status = "baseline"
                if key_baseline and diff.existing > 0 \
                        and diff.removed / diff.existing > settings.suspect_removal_ratio:
                    # The new key returned far fewer rows (a wrong key, an outage): keep the
                    # old rows (upsert only). The flag stays for one more silent try; after a
                    # second short fetch it is cleared, so the next run's removal guard holds
                    # the removals for review instead of the source staying silent forever.
                    apply_diff(session, source.id, run_id, False, record_changes=False)
                    # First, so the 2000-character cut of run.error can't drop it.
                    warnings.insert(0, f"{SKIPPED_DELETIONS}: {diff.removed} rows "
                                       "missing (over the safety limit)")
                    if _skipped_deletions_before(session, source.id, run_id):
                        session.get(Source, source.id).needs_baseline = False
                    diff = replace(diff, removed=0)  # nothing was removed; the warning has N
                else:
                    apply_diff(session, source.id, run_id, parsed.complete, record_changes=False)
                    if key_baseline:
                        # Cleared by any non-failed key baseline, even an incomplete one (it
                        # deleted nothing): a flag that waits for a complete run could keep
                        # a source silent forever.
                        session.get(Source, source.id).needs_baseline = False
            elif (
                (diff.existing > 0 and diff.removed / diff.existing > settings.suspect_removal_ratio)
                or (force_hold and diff.added + diff.altered + diff.removed > 0)
            ):
                status = "suspect_pending"
                _supersede_held(session, source.id, except_id=run_id)
                record_pending(session, source.id, run_id, parsed.complete, excl)
            elif diff.added + diff.altered + diff.removed == 0:
                status = "no_change"
                if excl:  # silent baseline rows still need storing
                    apply_diff(session, source.id, run_id, parsed.complete, record_changes=False,
                               exclude_indicators=excl)
            else:
                apply_diff(session, source.id, run_id, parsed.complete, record_changes=True,
                           exclude_indicators=excl)
                status = "ok"
            _finish(session, run, status, diff, parsed.indicators, parsed.vintage, warnings)
        except Exception as e:  # noqa: BLE001
            run = _fail(session, run_id, e, cred)
            _on_failed(session, connector, run)
            _notify(session, run, notifier, connector)
            return _detach(session, run)

        _after_run(session, connector, run)
        _notify(session, run, notifier, connector)
        return _detach(session, run)
    finally:
        session.close()
        if release is not None:
            release()


def approve_run(session_factory: SessionFactory, run_id: int, notifier: Notifier | None = None) -> Run:
    session = session_factory()
    release = None
    try:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"run {run_id} not found")
        source_id = run.source_id
        session.rollback()  # end the read transaction; state is re-read under the lock
        release = _acquire_source_lock(session, source_id)  # SourceBusyError if busy
        run = session.get(Run, run_id)
        session.refresh(run)
        latest = session.scalar(
            select(func.max(Run.id)).where(
                Run.source_id == run.source_id, Run.status.in_(HELD_STATUSES)
            )
        )
        if run.status not in HELD_STATUSES or latest != run.id:
            raise ValueError(f"run {run_id} is not the latest held (suspect_pending/approve_queued) run")
        if not run.raw_path:
            raise ValueError(f"run {run_id} has no raw file")
        source = session.get(Source, run.source_id)
        connector = resolve_connector(session, source.slug)
        try:
            parsed = connector.parse(Path(run.raw_path))
            stage_rows(session, _canonical_rows(parsed.rows))
            diff = compute_diff(session, source.id, parsed.complete, parsed.baseline_indicators)
            apply_diff(session, source.id, run.id, parsed.complete, record_changes=True,
                       exclude_indicators=parsed.baseline_indicators)
            run.trigger = "approve"
            run.error = None
            # The raw file is the single source of truth: changes were just recorded from it.
            session.execute(delete(PendingChange).where(PendingChange.run_id == run.id))
            _finish(session, run, "ok", diff, parsed.indicators, parsed.vintage, parsed.warnings)
        except Exception:
            session.rollback()
            raise
        _after_run(session, connector, run)  # e.g. watch targets are baselined once applied
        _notify(session, run, notifier)
        return _detach(session, run)
    finally:
        session.close()
        if release is not None:
            release()


def reject_run(session_factory: SessionFactory, run_id: int) -> Run:
    session = session_factory()
    release = None
    try:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"run {run_id} not found")
        source_id = run.source_id
        session.rollback()
        release = _acquire_source_lock(session, source_id)  # SourceBusyError if busy
        run = session.get(Run, run_id)
        session.refresh(run)
        if run.status != "suspect_pending":
            raise ValueError(f"run {run_id} is not suspect_pending")
        run.status = "rejected"
        run.finished_at = _now()
        session.execute(delete(PendingChange).where(PendingChange.run_id == run.id))
        session.commit()
        return _detach(session, run)
    finally:
        session.close()
        if release is not None:
            release()
