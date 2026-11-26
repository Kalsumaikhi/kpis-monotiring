"""Mattermost incoming-webhook notifications for run outcomes.

One message per run, except `no_change`, `rejected` and `superseded`, which are
silent unless a login moved into "login needed" during the run. A missing webhook URL disables sending without raising.
"""

from __future__ import annotations

import httpx

from app.config import get_settings
from app.core.pipeline import SKIPPED_DELETIONS
from app.models import Run, Source

_TIMEOUT_SECONDS = 10
_MAX_ERROR_CHARS = 200
_MAX_INDICATORS = 5
_MAX_EXCERPT_LINES = 5


def _link(base_url: str, run: Run) -> str:
    return f"{base_url.rstrip('/')}/runs/{run.id}"


def _first_line(text: str | None) -> str:
    line = (text or "").strip().splitlines()
    return line[0][:_MAX_ERROR_CHARS] if line else ""


def format_run_message(
    run: Run,
    source: Source,
    top_indicators: list[tuple[str, int]],
    streak: int,
    *,
    dashboard_base_url: str | None = None,
    consecutive_failure_alert: int | None = None,
    extras: dict | None = None,
) -> str | None:
    """Return the message text for a run, or None when nothing should be sent.

    extras (built by the pipeline): page_kpis (names of changed page KPIs), excerpt
    ({added, removed} of the first one), api_counts ((added, altered, removed) of the
    non-page rows), incomplete_note (set only when the run became incomplete) and
    login_needed ([{login_name, source}] of logins that now need a sign-in; one message
    line each, for every run status).
    """
    text = _base_message(
        run, source, top_indicators, streak, dashboard_base_url, consecutive_failure_alert,
        extras or {},
    )
    blocks = [text] if text is not None else []
    note = (extras or {}).get("incomplete_note")
    if note and run.status in ("ok", "no_change", "baseline"):
        blocks.append(f":warning: **{source.name}** — incomplete fetch: "
                      f"{_no_mentions(note[:_MAX_ERROR_CHARS])}")
    skipped = _skipped_deletions(run)
    if skipped:
        blocks.append(f":warning: **{source.name}** — {_no_mentions(skipped[:_MAX_ERROR_CHARS])}")
    # Logins that moved into login_needed during this run: whatever the run's status.
    base = get_settings().dashboard_base_url if dashboard_base_url is None else dashboard_base_url
    seen: set[str] = set()
    for item in (extras or {}).get("login_needed") or []:
        if item["login_name"] in seen:
            continue
        seen.add(item["login_name"])
        blocks.append(_login_needed_block(item["source"], item["login_name"], base))
    return "\n\n".join(blocks) if blocks else None


def _skipped_deletions(run) -> str | None:
    """The key baseline's "skipped deletions" warning, if this baseline run has one."""
    if run.status != "baseline":
        return None
    for part in (run.error or "").split("; "):
        if part.startswith(SKIPPED_DELETIONS):
            return part
    return None


def _login_needed_block(source_name: str, login_name: str, base_url: str) -> str:
    return (f"**{_no_mentions(source_name)}** — {_no_mentions(login_name)} needs you to sign in. "
            "Open the dashboard to enter the email code.\n"
            f"[Sign in]({base_url.rstrip('/')}/settings#logins)")


def _ok_block(name: str, added: int, altered: int, removed: int, vintage: str | None,
              top_indicators: list[tuple[str, int]], link: str) -> str:
    lines = [f"**{name}** — {added:,} rows added · {altered:,} values revised · {removed:,} removed"]
    if vintage:
        lines.append(f"Release: {vintage}")
    if top_indicators:
        shown = ", ".join(f"{code} ({n:,})" for code, n in top_indicators[:_MAX_INDICATORS])
        lines.append(f"Top indicators: {shown}")
    lines.append(f"[View changes]({link})")
    return "\n".join(lines)


_ZWSP = "\u200b"


def _inline_code(text: str) -> str:
    """A name as inline code: no Markdown formatting and no @mentions."""
    return "`" + " ".join(text.split()).replace("`", "'") + "`"


def _no_mentions(text: str) -> str:
    return text.replace("@", "@" + _ZWSP)


def _excerpt_line(text: str) -> str:
    """Untrusted page text inside a ```diff fence: one line, no fence breaks."""
    return " ".join(text.splitlines()).replace("```", "`" + _ZWSP + "``")


def _page_block(name: str, extras: dict, link: str) -> str:
    kpis = extras["page_kpis"]
    shown = ", ".join(_inline_code(k) for k in kpis[:_MAX_INDICATORS])
    lines = [f"**{name}** — {len(kpis)} KPI page(s) updated: {shown}"]
    excerpt = extras.get("excerpt") or {}
    diff = [f"+ {_excerpt_line(x)}" for x in excerpt.get("added", [])[:_MAX_EXCERPT_LINES]]
    diff += [f"- {_excerpt_line(x)}" for x in excerpt.get("removed", [])[:_MAX_EXCERPT_LINES]]
    if diff:
        lines += ["```diff", *diff, "```"]
    lines.append(f"[View changes]({link})")
    return "\n".join(lines)


def _base_message(run, source, top_indicators, streak, dashboard_base_url,
                  consecutive_failure_alert, extras) -> str | None:
    settings = get_settings()
    base = settings.dashboard_base_url if dashboard_base_url is None else dashboard_base_url
    threshold = (
        settings.consecutive_failure_alert
        if consecutive_failure_alert is None
        else consecutive_failure_alert
    )
    link = _link(base, run)
    name = source.name

    if run.status == "ok":
        if extras.get("page_kpis"):
            blocks = [_page_block(name, extras, link)]
            added, altered, removed = extras.get("api_counts", (0, 0, 0))
            if added + altered + removed:
                blocks.append(_ok_block(name, added, altered, removed, run.vintage,
                                        top_indicators, link))
            return "\n\n".join(blocks)
        return _ok_block(name, run.added, run.altered, run.removed, run.vintage,
                         top_indicators, link)

    if run.status == "baseline":
        if source.access == "watch":
            return None  # custom sources: first check is silent (R18)
        return f"**{name}** — baseline loaded: {run.rows_total:,} rows"

    if run.status == "failed":
        lines = [
            f":warning: **{name}** — check failed: {_first_line(run.error)}",
            f"[View run]({link})",
        ]
        if streak >= threshold:
            lines.append(f"Failed {streak} times in a row.")
        return "\n".join(lines)

    if run.status == "suspect_pending":
        existing = run.rows_total - run.added + run.removed
        return (
            f":warning: **{name}** — {run.removed:,} of {existing:,} rows would be removed. "
            "Changes held for review.\n"
            f"[Review]({link})"
        )

    return None


class MattermostNotifier:
    def __init__(self, webhook_url: str | None, dashboard_base_url: str,
                 consecutive_failure_alert: int) -> None:
        self.webhook_url = webhook_url
        self.dashboard_base_url = dashboard_base_url
        self.consecutive_failure_alert = consecutive_failure_alert

    def notify_run(
        self, run: Run, source: Source, top_indicators: list[tuple[str, int]], streak: int,
        extras: dict | None = None,
    ) -> None:
        if not self.webhook_url:
            return
        text = format_run_message(
            run, source, top_indicators, streak,
            dashboard_base_url=self.dashboard_base_url,
            consecutive_failure_alert=self.consecutive_failure_alert,
            extras=extras,
        )
        if text is None:
            return
        response = httpx.post(self.webhook_url, json={"text": text},
                              timeout=_TIMEOUT_SECONDS)
        response.raise_for_status()


def notifier_from_settings() -> MattermostNotifier:
    settings = get_settings()
    return MattermostNotifier(
        settings.mattermost_webhook_url,
        settings.dashboard_base_url,
        settings.consecutive_failure_alert,
    )
