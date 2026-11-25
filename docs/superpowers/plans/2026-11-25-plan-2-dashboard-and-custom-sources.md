# KPI Monitor — Plan 2: Dashboard & Custom Sources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Vue 3 dashboard (spec §5.8) over the Plan 1 backend, with KPI change frequency (§5.4), reviewable held runs, uploads (§5.7) and custom watched sources (web pages and API links) added by Excel import or a settings form (§5.9).

**Architecture:** The backend gains new tables (watch targets and snapshots, pending changes, a change rollup, import previews), a database-registered `WatchConnector` that plugs into the existing pipeline, and new read endpoints. Held runs store their would-be changes in `pending_changes` so review is an indexed read. Approve is queued to the worker instead of running inside the HTTP request. The frontend is a Vite + Vue 3 + TypeScript SPA served by nginx, which proxies `/api` to the api container. All colours come from one token file.

**Tech Stack:** Backend: Python 3.12, FastAPI, SQLAlchemy 2, Alembic, httpx, beautifulsoup4 (html.parser), openpyxl, Playwright for Python (optional; used only when `use_browser` is set). Frontend: Vue 3.5, Vite 6, TypeScript 5, vue-router 4, Chart.js 4 + vue-chartjs 5, Vitest 3 + @vue/test-utils, Playwright (node) for one end-to-end smoke test.

**Spec:** `docs/superpowers/specs/2026-11-24-kpi-monitor-design.md` (§5.4, §5.7, §5.8, §5.9, colours). Plan 1: `docs/superpowers/plans/2026-11-24-plan-1-pipeline-and-alerts.md`.

**Base branch:** `feat/plan-2-dashboard`, branched from `feat/plan-1-pipeline` (its PR is not merged yet).

## Global Constraints

- **Colours** (spec, HEX used on screen):

  | Name | HEX |
  |---|---|
  | Dark Green | `#063B2B` |
  | Spring Green | `#05EA7D` |
  | Mid Green | `#00874F` |
  | Light Green | `#E8F2DD` |

  - Dark Green: sidebar/header background and primary text on light surfaces.
  - Mid Green: buttons and links on white, with white text on Mid Green buttons.
  - Spring Green: accent only on Dark Green (active nav item). Never use it for text or thin lines on white or Light Green.
  - Light Green: page/card tint.
  - Status colours: amber = suspect/pending review, red = failed, Mid Green = ok/added.
  - Change types: added = Mid Green, altered = amber, removed = red.
  - Status is never shown by colour alone; always add an icon and a label.
  - Every colour is a CSS custom property in `frontend/src/styles/tokens.css`, with a light theme and a dark theme (`prefers-color-scheme` plus `[data-theme]` override). No component hard-codes a colour.
- Text contrast meets WCAG AA (4.5:1) in both themes.
- Tables are paginated and filtered server-side, with `page_size` at most 200.
- Auth: every `/api` route except `/api/health` uses HTTP Basic with any username and `DASHBOARD_PASSWORD`. The SPA asks for the password once and keeps it in `sessionStorage`. On a 401 it clears the password and returns to the login screen.
- Run links use `/runs/{id}`, because Mattermost messages already link to `{DASHBOARD_BASE_URL}/runs/{id}`.
- Attribution: wherever a source's data is shown, its credit line from the API field `attribution` is displayed. Credit lines:
  - SIPRI: `Source: SIPRI Military Expenditure Database`
  - World Bank: `Source: World Bank, World Development Indicators (CC BY 4.0)`
  - IMF: `Source: IMF DataMapper`
- **Watch targets (§5.9):**
  - Fetching:
    - User agent `KPI-Monitor/1.0`.
    - Timeout 30 s, at most 1 request per second per host.
    - robots.txt is respected; a disallowed URL gets status `blocked`.
    - Download cap 5 MB, over which the target errors with `too large`.
  - Excerpt: up to 5 added lines and 5 removed lines, each 200 characters at most.
  - "Noisy" = changed in more than 80% of the last 10 checks.
  - Test link preview: first 2,000 characters of text, or first 50 JSON values.
  - Import columns are `Source name`, `KPI name` and `Link`, matched case- and space-insensitively; extra columns are ignored.
  - Deleting a target removes its data points without "removed" alerts.
- Mattermost format for page changes: `**{source}** — {n} KPI page(s) updated: {kpi names, max 5}` + `\n` + first excerpt + `\n[View changes]({link})`.
- Typical update interval = median gap in days between distinct UTC days on which the indicator had changes (needs at least 2 such days, otherwise `null`).

## Review Focus

1. **A page that embeds a clock or rotating banner:**
   - It changes on every check, gets flagged "noisy" after 10 checks, and never floods Mattermost with more than one message per run.
   - Test: Task 4 `test_noisy_flag_after_9_of_10_changes`; Task 6 test of one message per run.
2. **A messy import sheet:**
   - Headers like `source  NAME` / `Link `, blank rows, the same row twice in one file and a `ftp://` link should give one valid row, one in-file duplicate and one invalid row with its row number. Blank rows are ignored silently.
   - Test: Task 5 `test_import_preview_messy_sheet`.
3. **A link that returns a PDF or other binary content:**
   - Treated as a page whose content is the sha256 of the bytes. The excerpt says `binary content changed`, and nothing crashes.
   - Test: Task 4 `test_binary_content_hashed`.
4. **An expired or wrong dashboard password mid-session:**
   - A 401 on any request returns to login with the message `Password incorrect or expired`. No request loop.
   - Test: Task 7 `api client clears password and redirects on 401`.
5. **Approving a held run twice, or while the source is busy:**
   - The second approve gets 409. Approve returns 202 at once and the worker applies it.
   - Test: Task 2 `test_approve_is_queued_and_double_approve_409`.

---

## File Structure

```
backend/
  migrations/versions/0003_dashboard_and_watch.py
  app/models.py                       + WatchTarget, WatchSnapshot, PendingChange, ChangeRollup, ImportPreview
  app/connectors/registry.py          + resolve_connector(session, slug)
  app/connectors/watch/
    __init__.py
    fetch.py                          robots, rate limit, size cap, content-type routing, browser option
    extract.py                        html → normalised text (selector/main heuristic); json → flat leaves
    connector.py                      WatchConnector (Connector protocol over DB targets)
    excerpt.py                        line diff → excerpt
  app/core/pipeline.py                pending_changes on hold; approve via queue; force_hold for uploads; rollup refresh
  app/core/rollup.py                  refresh_rollup(session, run_id); kpi_frequency queries
  app/notify/mattermost.py            + watch-source format, incomplete-fetch note
  app/worker.py                       + approve_queued polling; watch sources scheduled
  app/api/routes_kpis.py              frequency + explorer endpoints
  app/api/routes_watch.py             custom sources, targets, test link, import
  app/api/routes_uploads.py           upload → held run
  app/api/routes_sources.py           + attribution, incomplete flag, noisy count
  app/api/routes_runs.py              changes for pending runs read pending_changes; excerpts
frontend/
  package.json, vite.config.ts, tsconfig.json, index.html, Dockerfile, nginx.conf
  src/main.ts, src/App.vue, src/router.ts
  src/styles/tokens.css, src/styles/base.css
  src/api/client.ts                   fetch wrapper + auth + typed endpoint functions
  src/api/types.ts                    response types (mirror the API shapes below)
  src/components/                     AppShell.vue, StatusBadge.vue, DataTable.vue, Pager.vue, Sparkline.vue, Attribution.vue, EmptyState.vue, ConfirmDialog.vue
  src/pages/                          LoginPage, OverviewPage, ChangeFeedPage, RunDetailPage, ExplorerPage, KpiFrequencyPage, UploadsPage, SettingsPage (+ settings/BuiltinSources.vue, settings/CustomSources.vue, settings/ImportDialog.vue, settings/TargetForm.vue)
  tests/unit/*.spec.ts, tests/e2e/smoke.spec.ts
docker-compose.yml                    + web service (nginx, port 8080)
```

---

### Task 1: Schema for Plan 2 and connector resolution from the database

**Files:**
- Create: `backend/migrations/versions/0003_dashboard_and_watch.py`, `backend/tests/test_schema_plan2.py`
- Modify: `backend/app/models.py`, `backend/app/connectors/registry.py`, `backend/app/core/pipeline.py` (use `resolve_connector`), `backend/app/worker.py` (`reload_jobs` uses `resolve_connector`)

**Interfaces:**
- Produces:
  - Tables. All ids are bigint identity, and every FK cascades on delete from `sources` / `watch_targets`.
    - `watch_targets`:
      - `id`, `source_id` FK
      - `kpi_name` text, `url` text, `kind` text default `'auto'`, `selector` text null
      - `use_browser` bool default false, `enabled` bool default true
      - `last_status` text default `'never'`, `last_checked_at` timestamptz null, `last_error` text null
      - `created_at`
      - unique `(source_id, url)`
    - `watch_snapshots(id, target_id FK, run_id FK null, content text, content_hash char(64), changed bool, created_at)`, indexed on `(target_id, id desc)`.
    - `pending_changes`: same columns as `changes`, indexed on `(run_id)`.
    - `change_rollup`:
      - `source_id`, `indicator`, `day` date, `added` int, `altered` int, `removed` int
      - PK `(source_id, indicator, day)`
    - `import_previews(token text PK, rows jsonb, created_at)`.
    - Index on `changes(source_id, created_at)`.
  - `Source.access` can also be `'watch'` or `'manual'`.
  - `registry.resolve_connector(session: Session, slug: str) -> Connector`:
    - Returns the static `CONNECTORS[slug]` if it exists.
    - Otherwise, if the source row has `access == 'watch'`, returns `WatchConnector(source_id, session_factory_from(session))`. For this task, `WatchConnector` is a stub class in `app/connectors/watch/connector.py` that raises `NotImplementedError` in `download`.
    - Otherwise raises `KeyError`.
  - `pipeline.run_source`, `approve_run` and `preview_changes` call `resolve_connector`. `worker.reload_jobs` schedules any enabled source that `resolve_connector` accepts.

- [ ] **Step 1: Write the failing tests:**
  - `test_upgrade_creates_plan2_tables` asserts the 5 new tables and the `changes(source_id, created_at)` index exist.
  - `test_resolve_connector_static_and_watch` checks two cases. A static slug returns the same object as `CONNECTORS`. A source row with `access='watch'` returns a `WatchConnector` whose `slug` equals the source slug, `raw_suffix == ".json"` and `access == "watch"`. An unknown slug raises `KeyError`.
  - `test_reload_jobs_schedules_watch_source`.
- [ ] **Step 2: Run them.** Command: `cd backend && TEST_DATABASE_URL=postgresql+psycopg://kpi:kpi@localhost:5432/kpi_test uv run pytest tests/test_schema_plan2.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the migration (autogenerate, then review it; the downgrade drops everything it adds), the models, `resolve_connector` and the call-site changes.
- [ ] **Step 4: Run the full suite.** Command: `uv run pytest -m "not slow"`. Expected: everything passes.
- [ ] **Step 5: Commit.** `feat: plan 2 schema and DB-resolved connectors`

---

### Task 2: Held runs store pending changes; approve is queued; uploads can force a hold

**Files:**
- Modify: `backend/app/core/diff.py`, `backend/app/core/pipeline.py`, `backend/app/worker.py`, `backend/app/api/routes_runs.py`
- Test: `backend/tests/test_pending.py`

**Interfaces:**
- Consumes: Task 1 tables.
- Produces:
  - `diff.record_pending(session, source_id: int, run_id: int, complete: bool) -> None`: inserts the would-be changes from `staging` into `pending_changes`, using the same SELECT as the `changes` insert in `apply_diff`. It never touches `data_points`.
  - `run_source(..., force_hold: bool = False)`:
    - When a run would be `suspect_pending`, or when `force_hold` is set and the run has changes, it calls `record_pending` and sets status `suspect_pending`.
    - A baseline is never held, even with `force_hold`.
  - New run status `approve_queued`.
  - `POST /api/runs/{id}/approve`:
    - Moves a `suspect_pending` run that is the latest held run of its source to `approve_queued` with an atomic `UPDATE … WHERE status='suspect_pending'`, and returns 202 `{"run_id": id, "status": "approve_queued"}`.
    - Any other state returns 409.
  - `worker.poll_queued_once` also picks `approve_queued` runs and calls `approve_run`:
    - `approve_run` accepts `approve_queued` as well as `suspect_pending`.
    - It applies the held changes by copying `pending_changes` to `changes` and applying the diff from the raw file as today, then deletes that run's `pending_changes` rows.
    - On `SourceBusyError` the run stays `approve_queued`. On any other error the run goes back to `suspect_pending` with `error` set.
  - `reject_run` and supersede delete the run's `pending_changes`.
  - `GET /api/runs/{id}/changes` for a `suspect_pending` or `approve_queued` run reads `pending_changes` (indexed, same filters and shape) and adds `"pending": true`. `preview_changes` and its raw-file re-parse are removed.

- [ ] **Step 1: Write the failing tests:**
  - `test_held_run_writes_pending_changes`: after a baseline of 10 rows and a fetch of 7, there are 3 `removed` rows in `pending_changes`, `data_points` is unchanged, and `changes` is empty for the run.
  - `test_force_hold_holds_small_change`: one altered row with `force_hold=True` → `suspect_pending` with 1 pending row.
  - `test_force_hold_never_holds_baseline`.
  - `test_approve_is_queued_and_double_approve_409`: the first POST returns 202 with status `approve_queued`, and a second POST returns 409.
  - `test_worker_applies_approve_queued`: a poll tick leaves the run `ok`, its pending rows moved to `changes` and `data_points` updated.
  - `test_reject_and_supersede_clear_pending`.
  - `test_changes_endpoint_reads_pending`.
- [ ] **Step 2: Run them.** Command: `uv run pytest tests/test_pending.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Remove `preview_changes` and its API tests. Update the tests that expected synchronous approve.
- [ ] **Step 4: Run the full suite.** Expected: everything passes.
- [ ] **Step 5: Commit.** `feat: pending changes for held runs, queued approve, force-hold`

---

### Task 3: Change rollup and KPI frequency and explorer endpoints

**Files:**
- Create: `backend/app/core/rollup.py`, `backend/app/api/routes_kpis.py`, `backend/tests/test_kpis.py`
- Modify: `backend/app/core/pipeline.py` (call `refresh_rollup` after a run applies changes, in the same transaction), `backend/app/api/main.py` (router), `backend/app/api/routes_sources.py` (`attribution`), `backend/app/connectors/{worldbank_wdi,imf_weo,sipri_milex}.py` (`attribution` attribute with the exact strings from Global Constraints)

**Interfaces:**
- Produces:
  - `rollup.refresh_rollup(session, run_id: int) -> None`: upserts per-day counts from `changes` for that run, grouped by `(source_id, indicator, created_at::date)`.
  - `rollup.backfill(session) -> int`: rebuilds the rollup from all `changes`. Migration 0003 does not call it; expose it as `python -m app.cli backfill-rollup`.
  - `GET /api/kpis?source=&q=&sort=changes_30d|changes_12m|last_changed|typical_interval&order=desc|asc&page=&page_size=` returns `{items: [{source, indicator, name, unit, changes_30d, changes_12m, changes_all, first_seen, last_changed, typical_interval_days, sparkline}], total}`:
    - `sparkline` is a list of 12 ints, the monthly change counts for the last 12 months, oldest first.
    - `first_seen` is the earliest `data_points.first_seen_at` for the indicator.
    - All of it is computed from `change_rollup` and `indicators`, never by scanning `changes`.
  - `GET /api/sources/{slug}/indicators?q=&page=&page_size=` returns `{items: [{code, name, unit, points}], total}`.
  - `GET /api/sources/{slug}/indicators/{code}/countries?q=&page=&page_size=` returns `{items: [{country, points, last_changed}], total}`.
  - `GET /api/sources/{slug}/series?indicator=&country=` returns:
    - `points`: `[{period, dims, value}]`, sorted by period with `value` as a string;
    - `history`: `[{run_id, created_at, change_type, period, dims, old, new}]`, at most 500 rows, newest first;
    - `attribution`.
  - `/api/sources` items gain `attribution` (or `null`), `incomplete` (true when the latest finished run's `error` starts with `incomplete:` and its status is not `failed`) and `noisy_targets` (int; 0 for sources that aren't watch sources; Task 4 fills it).

- [ ] **Step 1: Write the failing tests:**
  - `test_rollup_counts_per_day`
  - `test_kpis_frequency_fields`: changes seeded on days D, D+10 and D+30 give `typical_interval_days == 15` (the median of 10 and 20), `changes_all == 3`, and a sparkline of length 12 that sums to 3.
  - `test_typical_interval_null_with_one_day`
  - `test_kpis_sort_and_filter`
  - `test_series_points_and_history`
  - `test_sources_have_attribution_and_incomplete`
- [ ] **Step 2: Run them.** Command: `uv run pytest tests/test_kpis.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Compute the median interval in SQL (`percentile_cont(0.5)` over the gaps between distinct days) or in Python over that indicator's distinct days only.
- [ ] **Step 4: Run them again,** then the full suite. Then run it on the dev DB: `DATABASE_URL=…/kpi uv run python -m app.cli backfill-rollup`, and time `/api/kpis?page_size=50`. Expected: under 1 s.
- [ ] **Step 5: Commit.** `feat: change rollup, KPI frequency and explorer endpoints`

---

### Task 4: WatchConnector (fetch, extract, snapshots, excerpt)

**Files:**
- Create: `backend/app/connectors/watch/{fetch.py,extract.py,excerpt.py}`, `backend/tests/test_watch_*.py`, `backend/tests/fixtures/watch/` (a few small HTML/JSON files)
- Modify: `backend/app/connectors/watch/connector.py`, `backend/pyproject.toml` (`beautifulsoup4`; optional extra `browser = ["playwright"]`), `backend/Dockerfile` (install the browser extra and `playwright install --with-deps chromium`)

**Interfaces:**
- Consumes: `Connector` protocol, `ParseResult(rows, complete, indicators, vintage, warnings)`, `RawRow`; the Task 1 tables.
- Produces:
  - `fetch.fetch_target(client: httpx.Client, url: str, use_browser: bool = False) -> FetchResult(status: 'ok'|'blocked'|'error', content_type: str, body: bytes, error: str|None)`. It enforces robots.txt (cached per host per run), 1 request per second per host, the 5 MB cap and the 30 s timeout. The browser path uses Playwright when `use_browser` is set; the `KPI_CHROMIUM_PATH` env var optionally sets `executable_path`.
  - `extract.page_text(html: str, selector: str | None) -> str`: drops `script, style, noscript, nav, header, footer, aside, form`. A selector keeps only the matched elements, and if it matches nothing the result is the empty string plus an error. Otherwise it uses `main` or `article`, falling back to `body`. Whitespace is collapsed within lines, blank lines are dropped, and line order is kept.
  - `extract.flatten_json(data) -> dict[str, str]` with paths like `a.b[0].c`. Leaves are stringified; null leaves are kept as `"null"`.
  - `excerpt.make_excerpt(old: str, new: str) -> dict(added: list[str], removed: list[str])`, at most 5 lines each, each 200 characters at most.
  - `WatchConnector(source_id, session_factory)`:
    - Attributes: `slug` (from the DB), `name`, `area = "custom"`, `access = "watch"`, `raw_suffix = ".json"`.
    - `download(client, dest)` fetches every enabled target and writes one JSON file: `{"targets": [{id, kpi_name, url, kind, status, content_type, text|json|sha256, error}]}`.
    - `parse(raw)`:
      - Pages produce `RawRow(dataset="page", indicator=kpi_name, country="", period="", value=sha256(text))`.
      - Binary content produces `value=sha256(bytes)` with text `"binary content changed"`.
      - APIs produce one row per leaf, `RawRow(dataset="api", indicator=kpi_name, country="", period="", value=leaf, dims={"path": path})`.
      - Blocked or errored targets add a warning and set `complete=False`. If every target errored, parse raises `RuntimeError("all targets failed")`.
      - `vintage=None`. Indicators are one `IndicatorMeta(code=kpi_name, name=kpi_name)` per target.
  - Snapshot hook: after a run finishes, `pipeline` calls `connector.after_run(session, run)` if the connector defines it.
    - `WatchConnector.after_run` writes one `watch_snapshots` row per fetched target (`changed` = whether its hash differs from the previous snapshot) and updates the target's `last_status`, `last_checked_at` and `last_error`.
    - It keeps the last `RAW_RETENTION_RUNS` snapshots per target.
  - `watch.noisy_target_ids(session, source_id) -> set[int]`: targets with more than 8 `changed` among their last 10 snapshots. Task 3's `noisy_targets` count uses it.

- [ ] **Step 1: Write the failing tests:**
  - `test_page_text_main_and_selector`: on fixture HTML with nav, footer and main, the text contains the main paragraphs and not the nav links. Selector `#kpi` keeps only that element, and an unmatched selector reports an error.
  - `test_flatten_json_paths`: `{"a":{"b":[{"c":1}]},"d":null}` → `{"a.b[0].c":"1","d":"null"}`.
  - `test_excerpt_limits`: 12 added lines → 5 kept. A 300-character line is cut to 200.
  - `test_robots_disallow_blocks`: respx serves a robots.txt with `Disallow: /private`, so `/private/x` → `blocked` and the URL itself is never requested.
  - `test_size_cap`: a 6 MB body → error `too large`.
  - `test_binary_content_hashed`: content type `application/pdf` → a page row whose value is the sha256, with excerpt text `binary content changed`.
  - `test_parse_partial_failure_incomplete`: 2 targets, one with a 500 → one row, `complete=False` and a warning naming the KPI.
  - `test_all_targets_failed_raises`.
  - `test_snapshots_and_noisy_flag_after_9_of_10_changes`: run the pipeline 10 times with content that changes in 9 → target in `noisy_target_ids`; with 8 changes → not.
  - `test_pipeline_end_to_end_watch_source`: baseline, then a page change → run `ok` with `altered=1` for that KPI.
- [ ] **Step 2: Run them.** Command: `uv run pytest tests/test_watch_*.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Use `bs4` with `html.parser`. Decode with the response charset, falling back to utf-8 with `errors="replace"`. `auto` kind is `api` when the content type contains `json`, otherwise `page`. The browser path is not unit-tested; cover it with one smoke run in Step 4.
- [ ] **Step 4: Run the full suite.** Then do a browser smoke run: `KPI_CHROMIUM_PATH=/opt/pw-browsers/chromium/... uv run --extra browser python -c "<fetch_target(url='https://example.com', use_browser=True)>"` and report the status. Find the real path with `ls /opt/pw-browsers`.
- [ ] **Step 5: Commit.** `feat: watch connector for web pages and API links`

---

### Task 5: Custom sources API (CRUD, test link, Excel/CSV import)

**Files:**
- Create: `backend/app/api/routes_watch.py`, `backend/app/watch_import.py`, `backend/tests/test_watch_api.py`
- Modify: `backend/app/api/main.py`

**Interfaces:**
- Consumes: the Task 4 `fetch_target`, `page_text`, `flatten_json`; the Task 1 models.
- Produces (all under `/api`, auth required):
  - `GET /api/custom-sources` → `[{slug, name, enabled, cron, targets: [{id, kpi_name, url, kind, selector, use_browser, enabled, last_status, last_checked_at, last_error, noisy}]}]`.
  - `POST /api/custom-sources` with body `{source_name, kpi_name, url, kind?, selector?, use_browser?}` → 201 target:
    - It creates the source if no custom source has that name: slug = `custom-` + slugified name (a numeric suffix is added on collision), `area='custom'`, `access='watch'`, default cron.
    - A non-http(s) URL or an empty name returns 422. A duplicate (source, url) returns 409.
  - `PATCH /api/watch-targets/{id}` updates any of `kpi_name, url, kind, selector, use_browser, enabled`.
  - `DELETE /api/watch-targets/{id}` → 204. It deletes the target and its `data_points` (dataset `page` / `api` with `indicator = kpi_name` of that source) without writing `changes`. When the last target goes, the source stays, with no targets.
  - `POST /api/watch-targets/test` with body `{url, kind?, selector?, use_browser?}` → `{status, kind, text_preview?: str (≤2000 chars), json_preview?: [{path, value}] (≤50), error?}`.
  - `POST /api/watch-targets/import/preview`, multipart `file` (`.xlsx` or `.csv`, at most 2 MB) → `{token, rows: [{row, source_name, kpi_name, url, status: 'new_source'|'existing_source'|'duplicate'|'invalid', error?}]}`. Data starts at row 2 (row 1 is the header).
    - A missing required column returns 422 `missing column: Link` (or whichever column is missing).
    - Blank rows are skipped.
    - Duplicate means the same source + URL either already exists or appears earlier in the file.
  - `POST /api/watch-targets/import/confirm` with body `{token}` → `{created_sources, created_targets, skipped}`. An unknown or expired token (older than 1 hour) returns 410. Tokens are single-use.
  - `watch_import.parse_sheet(filename: str, data: bytes) -> list[dict]` (header matching: lower-case, collapse spaces, strip).

- [ ] **Step 1: Write the failing tests:**
  - `test_create_target_creates_source_and_slug`
  - `test_create_target_rejects_bad_url_and_duplicate`
  - `test_delete_target_no_removed_changes`
  - `test_test_link_page_and_json` (respx)
  - `test_import_preview_messy_sheet`: an xlsx built in the test with headers `source  NAME`, `kpi name`, `Link ` and these rows:
    - a valid row,
    - a blank row,
    - the same valid row again,
    - a row with `ftp://x`,

    gives statuses `[new_source, duplicate, invalid]` with row numbers `[2, 4, 5]`.
  - `test_import_missing_column_422`
  - `test_import_confirm_creates_and_token_single_use`
  - `test_import_csv`
- [ ] **Step 2: Run them.** Command: `uv run pytest tests/test_watch_api.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.**
- [ ] **Step 5: Commit.** `feat: custom sources API with test link and Excel/CSV import`

---

### Task 6: Notifications for watch sources and incomplete fetches; uploads endpoint

**Files:**
- Modify: `backend/app/notify/mattermost.py`, `backend/app/core/pipeline.py` (pass `source.access` and page excerpts to the notifier)
- Create: `backend/app/api/routes_uploads.py`, `backend/tests/test_notify_plan2.py`, `backend/tests/test_uploads.py`

**Interfaces:**
- Produces:
  - `format_run_message` for an `ok` run of a source with `access == 'watch'`, where the changes are page rows:
    - First line: `**{source.name}** — {n} KPI page(s) updated: {names, max 5, comma-separated}`.
    - Then the first changed target's excerpt as lines prefixed `+ ` and `- `, at most 5 of each.
    - Last line: `[View changes]({link})`.
    - API rows on the same source use the normal ok format. If a run has both, both blocks are joined by a blank line.
  - Incomplete note: an `ok`, `no_change` or `baseline` run with an `incomplete:` warning, whose previous finished run of that source had none, also sends `:warning: **{source.name}** — incomplete fetch: {warning, ≤200 chars}`. No message if the previous run was already incomplete.
  - Run detail API: `GET /api/runs/{id}/changes` items for `dataset == "page"` include `excerpt: {added, removed}`, taken from the two latest snapshots at or before that run.
  - `POST /api/uploads`, multipart (`source` slug, `who`, `file`):
    - Only sources with `access == 'manual'` are accepted; otherwise 422.
    - Saves the file to the raw store, inserts an `uploads` row and a queued run with `trigger='upload'` and `raw_path` set.
    - The worker runs it with `from_raw` and `force_hold=True`, so the user reviews and approves it in Run detail.
    - Returns 202 `{run_id}`.
  - `GET /api/uploads?page=` lists uploads with their run status.
  - `GET /api/sources?access=manual` filters by access. Today no manual connectors exist, so the UI shows an empty state.

- [ ] **Step 1: Write the failing tests:**
  - `test_watch_page_message_format`: expects exactly `**My Source** — 2 KPI page(s) updated: GDP page, Budget page\n+ new line\n- old line\n[View changes](http://dash/runs/7)`.
  - `test_one_message_per_run_for_many_targets`
  - `test_incomplete_note_only_on_transition`
  - `test_page_change_excerpt_in_changes_api`
  - `test_upload_rejects_non_manual_source`
  - `test_upload_creates_queued_held_run`: a fake manual connector → after a worker tick, the run is `suspect_pending` with pending changes.
- [ ] **Step 2: Run them.** Command: `uv run pytest tests/test_notify_plan2.py tests/test_uploads.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** The worker's queued-run path must pass `from_raw=run.raw_path` and `force_hold=(run.trigger == 'upload')`.
- [ ] **Step 4: Run the full suite.**
- [ ] **Step 5: Commit.** `feat: watch-source notifications, incomplete-fetch note, uploads`

---

### Task 7: Frontend scaffold, tokens, shell, login and API client

**Files:**
- Create: everything under `frontend/` named in File Structure, except the pages that Tasks 9–13 own; `frontend/Dockerfile`, `frontend/nginx.conf`
- Modify: `docker-compose.yml` (add `web`: build `./frontend`, port `8080:80`, depends_on api healthy), `README.md` (dashboard section)

**Interfaces:**
- Produces:
  - `src/styles/tokens.css`:
    - Palette tokens: `--c-dark-green #063B2B`, `--c-spring-green #05EA7D`, `--c-mid-green #00874F`, `--c-light-green #E8F2DD`.
    - Semantic tokens: `--bg`, `--surface`, `--text`, `--text-muted`, `--border`, `--accent`, `--nav-bg`, `--nav-text`, `--nav-active`, `--status-ok`, `--status-warn` (amber), `--status-fail` (red), `--chg-added`, `--chg-altered`, `--chg-removed`, `--focus`.
    - Light values on `:root`. Dark values under `@media (prefers-color-scheme: dark)` with `:root:not([data-theme="light"])`, and again under `:root[data-theme="dark"]`.
    - The amber and red are chosen so that their text meets 4.5:1 on `--surface` in both themes. Record the values and ratios in a comment.
  - `src/api/client.ts`:
    - `setPassword(p)`, `clearPassword()`, `hasPassword()`.
    - `apiGet<T>(path, params?)`, `apiSend<T>(method, path, body?)`, `apiUpload<T>(path, formData)`.
    - Every request adds `Authorization: Basic base64("dashboard:" + password)`.
    - On a 401 it calls `clearPassword()`, sets `sessionStorage.loginMessage = "Password incorrect or expired"` and navigates to `/login?next=<current>`.
    - Other non-2xx responses throw `ApiError(status, detail)`.
  - `src/api/types.ts` holds types for every response shape in Plan 1 Task 10 and Plan 2 Tasks 2–6.
  - `AppShell.vue`: Dark Green sidebar with nav items Overview, Changes, Explorer, KPI frequency, Uploads, Settings. The active item uses `--nav-active` (Spring Green). On narrow screens the sidebar collapses to a top bar with a menu button. Includes a theme toggle (system/light/dark, stored in `localStorage`, read and written inside try/catch).
  - `StatusBadge.vue`, props `status: string`. It maps each run status to a label and icon:

    | Status | Label | Token |
    |---|---|---|
    | ok | OK | ok |
    | baseline | Baseline | ok |
    | no_change | No change | muted |
    | failed | Failed | fail |
    | suspect_pending | Needs review | warn |
    | approve_queued | Applying… | warn |
    | rejected | Rejected | muted |
    | superseded | Superseded | muted |
    | running | Running | muted, with a spinner |
    | queued | Queued | muted |
    | blocked | Blocked | warn |
    | never | Not checked yet | muted |

  - `DataTable.vue` (props `columns: {key, label, align?}[]`, `rows`, `loading`, `empty`; slots per cell) and `Pager.vue` (props `page`, `pageSize`, `total`; emits `update:page`).
  - `Sparkline.vue` (props `values: number[]`; inline SVG; `aria-label` "changes per month: …").
  - `Attribution.vue` (prop `text`).
  - `EmptyState.vue`, `ConfirmDialog.vue`.
  - Router routes:

    | Path | Page |
    |---|---|
    | `/login` | LoginPage |
    | `/` | OverviewPage |
    | `/changes` | ChangeFeedPage |
    | `/runs/:id` | RunDetailPage |
    | `/explorer/:slug?/:indicator?/:country?` | ExplorerPage |
    | `/kpis` | KpiFrequencyPage |
    | `/uploads` | UploadsPage |
    | `/settings` | SettingsPage |

    Every route except login redirects to `/login?next=` if there is no password. Pages not built yet are placeholder components.
  - `LoginPage`: one password field and a "Sign in" button. It checks the password with `GET /api/sources`; on 200 it stores the password and goes to `next`. It shows `sessionStorage.loginMessage` once.
  - `vite.config.ts` proxies `/api` to `http://localhost:8000` in dev.
  - `nginx.conf` serves the SPA with history fallback and proxies `/api/` to `http://api:8000`.
- **Before writing UI code, load the `frontend-design:frontend-design` and `dataviz` skills.** Follow them within the palette rules above.

- [ ] **Step 1: Scaffold.** Create the project with `npm create vite@latest frontend -- --template vue-ts` and pin exact versions in package.json. Add vue-router, chart.js, vue-chartjs, vitest, @vue/test-utils and jsdom.
- [ ] **Step 2: Write the failing unit tests:**
  - `client.spec.ts`:
    - `api client adds basic auth header`
    - `api client clears password and redirects on 401`: mock fetch to return 401; expect `hasPassword()` false, router at `/login`, and `loginMessage` `Password incorrect or expired`.
    - `api client throws ApiError with detail on 409`
  - `StatusBadge.spec.ts`: every status in the table renders its label and has an icon element.
  - `router.spec.ts`: an unauthenticated visit to `/kpis` redirects to `/login?next=/kpis`.
- [ ] **Step 3: Run them.** Command: `cd frontend && npx vitest run`. Expected: FAIL.
- [ ] **Step 4: Implement.**
- [ ] **Step 5: Verify.** Run `npx vitest run && npx vue-tsc --noEmit && npm run build`. Expected: all pass, and the build writes `dist/`. Validate compose with `docker compose config`.
- [ ] **Step 6: Commit.** `feat(frontend): scaffold, design tokens, shell, login, API client`

---

### Task 8: Contrast check for tokens

**Files:**
- Create: `frontend/tests/unit/contrast.spec.ts`

**Interfaces:**
- Consumes: `tokens.css`.

- [ ] **Step 1: Write the failing test.** `contrast.spec.ts` parses `tokens.css` (regex over custom properties per theme block) and asserts WCAG contrast of at least 4.5 for each of these pairs in light and dark: `text/bg`, `text/surface`, `text-muted/surface`, `accent/surface`, `status-ok/surface`, `status-warn/surface`, `status-fail/surface`, `nav-text/nav-bg`, `nav-active/nav-bg`. It also asserts that `--c-spring-green` is never the value of `--text`, `--accent` or any status token in the light theme.
- [ ] **Step 2: Run it.** Command: `npx vitest run tests/unit/contrast.spec.ts`. Expected: fail first if any token is off (adjust tokens until it passes; don't weaken the test).
- [ ] **Step 3: Commit.** `test(frontend): token contrast guard`

---

### Task 9: Overview page

**Files:**
- Create: `frontend/src/pages/OverviewPage.vue`, `frontend/tests/unit/OverviewPage.spec.ts`

**Interfaces:**
- Consumes: `GET /api/sources` (with `attribution`, `incomplete`, `noisy_targets`), `POST /api/sources/{slug}/run`.
- Produces:
  - Summary row: sources tracked, sources needing attention (failed, `failing_streak > 0`, needs review, incomplete) and changes in 30 days.
  - One card per source:
    - Name and area, StatusBadge of the last run, last successful run time (relative, with an absolute tooltip), last change, rows tracked (formatted with thousands separators), and changes in 30 days.
    - Flags: "Incomplete fetch" (warn), "{n} noisy pages" (warn, links to Settings), and "Failing {n}× in a row" (fail).
    - A "Run now" button that shows a toast on 202, "Already running" on 409 and "Busy" otherwise.
  - Sources needing attention are sorted first.

- [ ] **Step 1: Write the failing tests** with a mocked client:
  - `sorts attention sources first`
  - `shows incomplete and failing flags with labels`
  - `run now posts and shows toast`
  - `409 shows Already running`
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them again.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat(frontend): overview page`

---

### Task 10: Change feed and run detail (with review)

**Files:**
- Create: `frontend/src/pages/ChangeFeedPage.vue`, `frontend/src/pages/RunDetailPage.vue`, tests

**Interfaces:**
- Consumes: `GET /api/runs`, `GET /api/runs/{id}`, `GET /api/runs/{id}/changes`, `POST /api/runs/{id}/approve|reject`.
- Produces:
  - **Change feed:**
    - Filters: source, status, since date, and "only runs with changes" (on by default). Filters live in the URL query.
    - A paginated table: time, source, status, added / altered / removed (coloured numbers with +/~/− signs), and vintage.
    - A row click opens `/runs/:id`.
  - **Run detail:**
    - Header: source, status, times, trigger, vintage, counts, `error` (if any), and top indicators.
    - Changes table: filters for change type, indicator and country. Columns are type, indicator, country, period, dims, old → new.
    - For page rows, the excerpt is shown as `+`/`−` lines instead of old → new hashes.
    - When `pending` is true, a banner reads "These changes are held for review — {removed} of {existing} rows would be removed" with Approve and Reject buttons, both behind ConfirmDialog.
    - Approve: on 202 the badge shows "Applying…" and the page polls `GET /api/runs/{id}` every 5 s until the status leaves `approve_queued`. On 409 it shows the API detail.
    - Attribution footer.

- [ ] **Step 1: Write the failing tests:**
  - `feed filters sync to url`
  - `run detail renders pending banner and approves`: mock a 202, then a poll returns `ok` → the banner disappears.
  - `page change rows show excerpt lines`
  - `reject asks for confirmation`
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them again.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat(frontend): change feed and run detail with review`

---

### Task 11: Explorer and KPI frequency

**Files:**
- Create: `frontend/src/pages/ExplorerPage.vue`, `frontend/src/pages/KpiFrequencyPage.vue`, tests

**Interfaces:**
- Consumes: Task 3 endpoints.
- Produces:
  - **Explorer:**
    - A three-step drill-down (source → indicator with search → country with search) using route params.
    - At the last step: a line chart of the value series (Chart.js, one line per distinct `dims` set, at most 6 lines, colours from tokens) and a table of points.
    - Below it, change history (type, period, old → new, run link).
    - Non-numeric values show as a table only, with no chart.
    - Attribution footer.
  - **KPI frequency:**
    - A table with source filter and indicator search. Columns: indicator (name + code), source, changes 30d, 12m, all, last changed, typical interval (rendered as `≈ every N days`, or `≈ every N months` when N ≥ 60 with N/30 rounded, or `—` when null), and a Sparkline.
    - Sortable by the API's sort keys.
    - A row click opens Explorer at that indicator.

- [ ] **Step 1: Write the failing tests:**
  - `interval formatting`: `12 → "≈ every 12 days"`, `182 → "≈ every 6 months"`, `null → "—"`.
  - `explorer shows table only for text values`
  - `kpi table sort changes query`
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them again.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat(frontend): explorer and KPI frequency`

---

### Task 12: Settings (built-in sources and custom sources, form, test link, import)

**Files:**
- Create: `frontend/src/pages/SettingsPage.vue`, `frontend/src/pages/settings/{BuiltinSources,CustomSources,TargetForm,ImportDialog}.vue`, tests

**Interfaces:**
- Consumes: `PATCH /api/sources/{slug}`, run-now, the Task 5 endpoints.
- Produces:
  - **Built-in sources:**
    - A table with an enabled toggle and a cron field (validated server-side; a 422 shows "Invalid schedule") with helper presets (Daily 03:00 = `0 3 * * *`, Weekly Mon 03:00 = `0 3 * * 1`).
    - Run now.
  - **Custom sources:**
    - Grouped by source. Each target shows KPI name, link (opens in a new tab with `rel="noopener"`), StatusBadge of `last_status`, last checked, a "Noisy — set a focus area" chip when noisy, and Edit / Disable / Delete buttons (Delete behind ConfirmDialog with the text "Deleting removes this KPI's history. No alert is sent.").
    - Buttons "Add KPI" and "Import from Excel".
  - **TargetForm:**
    - Fields: source name (a combobox of existing custom source names, or free text), KPI name, link.
    - An "Advanced" disclosure with focus area (CSS selector), kind (auto/page/api) and "Needs a browser".
    - A "Test link" button shows the text preview (in a scrollable pre) or the JSON preview table, or `blocked`/`error` with its message.
    - Save is disabled until the name, KPI and an http(s) link are filled. 409 and 422 details show inline.
  - **ImportDialog:**
    - A file input (`.xlsx,.csv`) and a "Download template" link that generates a CSV client-side with the header `Source name,KPI name,Link`.
    - After upload, a preview table with row number, values and a status chip:
      - New source / Existing source: ok tone
      - Duplicate: muted, "skipped"
      - Invalid: fail tone, with the error
    - A summary line, and "Import {n} KPIs" (disabled when n = 0).
    - On confirm it shows the created counts and refreshes the list. A 410 shows "Preview expired — upload again".

- [ ] **Step 1: Write the failing tests:**
  - `form disables save until valid`
  - `test link shows text preview`
  - `import preview shows statuses and imports valid count`: mock preview rows `[new_source, duplicate, invalid]` → button text "Import 1 KPIs"; confirm posts the token.
  - `template download has header`
  - `delete asks confirmation with no-alert text`
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them again.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat(frontend): settings with custom sources, test link and Excel import`

---

### Task 13: Uploads page

**Files:**
- Create: `frontend/src/pages/UploadsPage.vue`, test

**Interfaces:**
- Consumes: `GET /api/sources?access=manual`, `POST /api/uploads`, `GET /api/uploads`.
- Produces:
  - With no manual sources, an EmptyState reads "No upload sources configured yet. Sources like TheGlobalEconomy.com are added once a sample export is available."
  - Otherwise, a form with source, your name and file, then "Upload". On 202 it links to the held run ("Review changes").
  - A list of past uploads with their status.

- [ ] **Step 1: Write the failing tests:**
  - `empty state without manual sources`
  - `upload posts form and links to run`
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them again.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat(frontend): uploads page`

---

### Task 14: End-to-end smoke test and screenshots

**Files:**
- Create: `frontend/tests/e2e/smoke.spec.ts`, `frontend/playwright.config.ts`
- Modify: `README.md`

- [ ] **Step 1: Start the stack locally** against the dev DB:
  - api: `uvicorn` with `DASHBOARD_PASSWORD=dev` on port 8000;
  - worker: `python -m app.worker`;
  - frontend: `npm run build && npx vite preview --port 8080`, with the preview proxy for `/api` configured in `vite.config.ts`.
- [ ] **Step 2: Write the Playwright smoke test** (uses the preinstalled Chromium; run with `--trace off`):
  1. Log in with a wrong password → error. Log in with `dev` → Overview lists 3 built-in sources.
  2. Open Changes → at least one run. Open it → Run detail renders.
  3. Explorer: `sipri-milex` → `constant-usd` → `France` → the chart canvas is visible.
  4. KPI frequency loads rows.
  5. Settings: add a custom KPI pointing at a local static HTML file served by a tiny server in the test, then Test link → the preview contains the page text. Save → it appears under the custom source. Run now → after the worker tick the target status is OK.
  6. Change the served HTML and run again → Changes shows a run for that source. Its detail shows the `+` excerpt line.
  7. Import: upload a CSV with 2 valid rows and 1 duplicate → the preview shows the statuses. Import → 2 created.
  8. Delete one imported KPI.
- [ ] **Step 3: Run it.** Command: `npx playwright test --trace off`. Expected: PASS. Take screenshots at deviceScaleFactor 2 of Overview, Run detail, KPI frequency and Settings (light theme, plus Overview dark), and save them to `docs/screenshots/`.
- [ ] **Step 4: Update the README:** dashboard URL (`http://localhost:8080`), login, the custom-sources import template, screenshots, and attribution notes.
- [ ] **Step 5: Run every suite.** Backend `pytest -m "not slow"`, frontend `vitest run`, `vue-tsc --noEmit`, the e2e test.
- [ ] **Step 6: Commit.** `test: end-to-end dashboard smoke test, screenshots, README`
