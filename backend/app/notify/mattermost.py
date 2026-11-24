"""Mattermost incoming-webhook notifications for run outcomes.

One message per run, except `no_change`, `rejected` and `superseded`, which are
silent. A missing webhook URL disables sending without raising.
"""

from __future__ import annotations

import httpx

from app.config import get_settings
from app.models import Run, Source

_TIMEOUT_SECONDS = 10
_MAX_ERROR_CHARS = 200
_MAX_INDICATORS = 5


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
) -> str | None:
    """Return the message text for a run, or None when nothing should be sent."""
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
        lines = [
            f"**{name}** — {run.added:,} rows added · {run.altered:,} values revised · "
            f"{run.removed:,} removed"
        ]
        if run.vintage:
            lines.append(f"Release: {run.vintage}")
        if top_indicators:
            shown = ", ".join(f"{code} ({n:,})" for code, n in top_indicators[:_MAX_INDICATORS])
            lines.append(f"Top indicators: {shown}")
        lines.append(f"[View changes]({link})")
        return "\n".join(lines)

    if run.status == "baseline":
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
        self, run: Run, source: Source, top_indicators: list[tuple[str, int]], streak: int
    ) -> None:
        if not self.webhook_url:
            return
        text = format_run_message(
            run, source, top_indicators, streak,
            dashboard_base_url=self.dashboard_base_url,
            consecutive_failure_alert=self.consecutive_failure_alert,
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
