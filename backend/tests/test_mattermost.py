import json

import httpx
import pytest
import respx

from app.models import Run, Source
from app.notify.mattermost import MattermostNotifier, format_run_message

BASE = "http://dash.test"
HOOK = "https://mm.test/hooks/abc"


def make_source(name="World Bank WDI"):
    return Source(slug="wb", name=name, area="a", access="api")


def make_run(**kw):
    fields = dict(
        id=42, source_id=1, status="ok", vintage=None,
        rows_total=1000, added=0, altered=0, removed=0, error=None,
    )
    fields.update(kw)
    return Run(**fields)


def fmt(run, top=(), streak=0, threshold=3, source=None):
    return format_run_message(
        run, source or make_source(), list(top), streak,
        dashboard_base_url=BASE, consecutive_failure_alert=threshold,
    )


def test_ok_message_exact():
    run = make_run(status="ok", added=1234, altered=5, removed=6)
    text = fmt(run, top=[("SP.POP.TOTL", 900), ("NY.GDP.MKTP", 334)])
    assert text == (
        "**World Bank WDI** — 1,234 rows added · 5 values revised · 6 removed\n"
        "Top indicators: SP.POP.TOTL (900), NY.GDP.MKTP (334)\n"
        "[View changes](http://dash.test/runs/42)"
    )


def test_ok_message_with_release_and_no_indicators_omits_indicator_line():
    run = make_run(status="ok", added=2, altered=0, removed=0, vintage="2026-Q3")
    text = fmt(run, top=[])
    assert text == (
        "**World Bank WDI** — 2 rows added · 0 values revised · 0 removed\n"
        "Release: 2026-Q3\n"
        "[View changes](http://dash.test/runs/42)"
    )


def test_ok_message_limits_indicators_to_five():
    run = make_run(status="ok", added=10)
    top = [(f"IND{i}", 10 - i) for i in range(7)]
    text = fmt(run, top=top)
    assert "Top indicators: IND0 (10), IND1 (9), IND2 (8), IND3 (7), IND4 (6)\n" in text
    assert "IND5" not in text
    assert "IND6" not in text


def test_baseline_message_exact():
    run = make_run(status="baseline", rows_total=12345)
    assert fmt(run) == "**World Bank WDI** — baseline loaded: 12,345 rows"


def test_failed_message_exact_without_streak_note():
    run = make_run(status="failed", error="HTTPError: 503\nsecond line")
    assert fmt(run, streak=1) == (
        ":warning: **World Bank WDI** — check failed: HTTPError: 503\n"
        "[View run](http://dash.test/runs/42)"
    )


def test_failed_message_truncates_first_line_to_200_chars():
    run = make_run(status="failed", error="x" * 500)
    text = fmt(run, streak=1)
    first = text.split("check failed: ", 1)[1].split("\n", 1)[0]
    assert first == "x" * 200


def test_failed_message_adds_streak_at_threshold():
    run = make_run(status="failed", error="boom")
    text = fmt(run, streak=3, threshold=3)
    assert text == (
        ":warning: **World Bank WDI** — check failed: boom\n"
        "[View run](http://dash.test/runs/42)\n"
        "Failed 3 times in a row."
    )


def test_failed_message_below_threshold_has_no_streak_note():
    run = make_run(status="failed", error="boom")
    assert "in a row" not in fmt(run, streak=2, threshold=3)


def test_suspect_pending_message_exact_and_existing_is_derived():
    # existing = rows_total - added + removed = 1000 - 50 + 300 = 1250
    run = make_run(status="suspect_pending", rows_total=1000, added=50, removed=300)
    assert fmt(run) == (
        ":warning: **World Bank WDI** — 300 of 1,250 rows would be removed. "
        "Changes held for review.\n"
        "[Review](http://dash.test/runs/42)"
    )


@pytest.mark.parametrize("status", ["no_change", "rejected", "superseded"])
def test_silent_statuses_return_none(status):
    assert fmt(make_run(status=status)) is None


def test_base_url_trailing_slash_is_stripped():
    run = make_run(status="baseline", rows_total=1)
    text = format_run_message(
        run, make_source(), [], 0, dashboard_base_url="http://dash.test/",
        consecutive_failure_alert=3,
    )
    assert text == "**World Bank WDI** — baseline loaded: 1 rows"
    link_run = make_run(status="suspect_pending", rows_total=10, added=0, removed=1)
    text = format_run_message(
        link_run, make_source(), [], 0, dashboard_base_url="http://dash.test/",
        consecutive_failure_alert=3,
    )
    assert text.endswith("[Review](http://dash.test/runs/42)")


@respx.mock
def test_notify_run_posts_text_to_webhook():
    route = respx.post(HOOK).mock(return_value=httpx.Response(200, text="ok"))
    notifier = MattermostNotifier(HOOK, BASE, 3)
    notifier.notify_run(make_run(status="baseline", rows_total=7), make_source(), [], 0)
    assert route.called
    assert json.loads(route.calls.last.request.content) == {
        "text": "**World Bank WDI** — baseline loaded: 7 rows"
    }


@respx.mock
def test_no_change_sends_nothing():
    route = respx.post(HOOK).mock(return_value=httpx.Response(200))
    notifier = MattermostNotifier(HOOK, BASE, 3)
    notifier.notify_run(make_run(status="no_change"), make_source(), [], 0)
    assert not route.called


def test_missing_webhook_is_noop():
    notifier = MattermostNotifier(None, BASE, 3)
    # No respx mock active: any real HTTP attempt would fail the test.
    assert notifier.notify_run(make_run(status="baseline", rows_total=1),
                               make_source(), [], 0) is None


@respx.mock
def test_http_500_raises():
    respx.post(HOOK).mock(return_value=httpx.Response(500))
    notifier = MattermostNotifier(HOOK, BASE, 3)
    with pytest.raises(httpx.HTTPStatusError):
        notifier.notify_run(make_run(status="baseline", rows_total=1),
                            make_source(), [], 0)


def test_key_baseline_skipped_deletions_warns():
    run = make_run(status="baseline", rows_total=1, error=(
        "key baseline skipped deletions: 9 rows missing (over the safety limit)"))
    assert fmt(run) == (
        "**World Bank WDI** — baseline loaded: 1 rows\n\n"
        ":warning: **World Bank WDI** — key baseline skipped deletions: 9 rows missing "
        "(over the safety limit)")
    run = make_run(status="baseline", rows_total=1, error="incomplete: x; key baseline "
                   "skipped deletions: 2 rows missing (over the safety limit)")
    assert fmt(run).endswith(
        ":warning: **World Bank WDI** — key baseline skipped deletions: 2 rows missing "
        "(over the safety limit)")
    assert fmt(make_run(status="baseline", rows_total=1, error="incomplete: x")) == (
        "**World Bank WDI** — baseline loaded: 1 rows")
