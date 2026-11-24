# KPI Monitor — Plan 1: Pipeline & Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Dockerised Python service that fetches World Bank WDI, IMF WEO and SIPRI military expenditure daily, detects added / altered / removed data points, stores history in Postgres, posts Mattermost summaries, and exposes a password-protected REST API for the dashboard (Plan 2).

**Architecture:** Every connector downloads one raw file and parses it into canonical rows `(dataset, indicator, country, period, dims) → value`. The pipeline canonicalises values, loads them into a Postgres staging table and diffs them set-based against `data_points`, writing `changes` and updating `data_points` in one transaction. Guards (baseline, suspect removal ratio) decide whether a run is applied, and the notifier posts one message per run. FastAPI serves the same database. The APScheduler worker is a second entrypoint of the same package.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 (sync), Alembic, psycopg 3, pandas, openpyxl, httpx, APScheduler 3.x, pytest, respx. Postgres 16. Docker Compose.

**Spec:** `docs/superpowers/specs/2026-11-24-kpi-monitor-design.md`

**Roadmap (separate plans):**
- Plan 2: Vue 3 dashboard (all pages in spec §5.8), KPI change-frequency rollup and endpoints (§5.4), manual upload flow (§5.7), palette tokens.
- Plan 3: the remaining connectors in spec §3, including the curated dataset lists for the large portals.

## Global Constraints

- Canonical key: `(source, dataset, indicator, country, period, dims)`; `period` is a string; `dims` is sorted JSON; forecast vintage is **not** in the key (it is stored on the run).
- Change types: `added`, `altered`, `removed`. `removed` is emitted only when the parse result has `complete=True`.
- Formatting noise must not produce `altered`: `"1.50"`, `1.5` and `1.5000000001` (beyond 10 significant digits) are equal.
- The first successful run of a source is a **baseline**: rows are loaded, no `changes` rows are written, and one Mattermost note `baseline loaded: N rows` is sent.
- Suspect guard: if `removed / existing_rows > SUSPECT_REMOVAL_RATIO` (default `0.2`), the run is not applied, its status is `suspect_pending`, and an alert is sent.
- Run statuses: `running`, `ok`, `no_change`, `failed`, `suspect_pending`, `rejected`, `superseded`, `baseline`.
- Mattermost: one message per run with changes. Messages also go out for failures, suspect runs, `CONSECUTIVE_FAILURE_ALERT` (default 3) failures in a row, and baselines. **No message for `no_change`.**
- Secrets and settings come only from env vars: `DATABASE_URL`, `MATTERMOST_WEBHOOK_URL`, `DASHBOARD_BASE_URL`, `DASHBOARD_PASSWORD`, `RAW_DIR`, `RAW_RETENTION_RUNS` (default 5), `SUSPECT_REMOVAL_RATIO`, `CONSECUTIVE_FAILURE_ALERT`.
- Default schedule: daily per source. It can be changed per source in the `sources` table (`cron` column).
- Respect each source's terms; never scrape. Before writing a connector, verify its URL and endpoint and record what you found in the connector's module docstring.

## Review Focus

1. **Download dies halfway (network error, truncated zip):** the run is `failed`, `data_points` is untouched, no `removed` rows are written, and Mattermost gets a failure message. Tests in Task 4 and Task 6.
2. **The same data is fetched twice:** zero changes, status `no_change`, no Mattermost message. Test in Task 4.
3. **Duplicate keys inside one file:** identical duplicates collapse. Conflicting duplicates fail the run with a message naming up to 5 keys, so the outcome never depends on row order. Tests in Task 3.
4. **A value turns into a missing marker** (`..`, empty, `n/a`, `NaN`): the point is treated as absent, so it shows as `removed` on a complete fetch. It is never `altered` to an empty value. Tests in Task 2 and Task 3.
5. **Mattermost is down or the webhook is not set:** the run still succeeds and is recorded, and the notification error is logged on the run (`notify_error`). Test in Task 5.

---

## File Structure

```
kpi-monitor/
  docker-compose.yml            db, api, worker (web added in Plan 2)
  .env.example
  backend/
    pyproject.toml
    Dockerfile
    alembic.ini
    migrations/                  Alembic env + versions
    app/
      config.py                  Settings (pydantic-settings)
      db.py                      engine, SessionLocal, get_session
      models.py                  ORM tables
      core/
        canonical.py             RawRow, CanonicalRow, canonicalize_value, key_hash, value_hash
        diff.py                  staging load + set-based diff + apply
        pipeline.py              run_source / approve_run / reject_run
        raw_store.py             raw file paths + retention
      connectors/
        base.py                  Connector protocol, ParseResult, IndicatorMeta
        registry.py              CONNECTORS dict, get_connector(slug)
        worldbank_wdi.py
        imf_weo.py
        sipri_milex.py
      notify/mattermost.py       format_* + send
      worker.py                  scheduler entrypoint
      cli.py                     `python -m app.cli run <slug>` / `sync-sources`
      api/
        main.py                  app factory, auth dependency
        routes_sources.py
        routes_runs.py
    tests/
      conftest.py                Postgres test DB fixture, fake connector
      fixtures/                  trimmed real sample files per connector
      test_*.py
```

---

### Task 1: Project scaffold, settings, schema

**Files:**
- Create: `backend/pyproject.toml`, `backend/Dockerfile`, `backend/alembic.ini`, `backend/migrations/env.py`, `backend/migrations/versions/0001_initial.py`, `backend/app/config.py`, `backend/app/db.py`, `backend/app/models.py`, `backend/tests/conftest.py`, `backend/tests/test_schema.py`, `docker-compose.yml`, `.env.example`, `.gitignore`

**Interfaces:**
- Produces:
  - `Settings` (pydantic-settings) with the fields in Global Constraints. `get_settings() -> Settings` is cached.
  - `SessionLocal`, `get_session() -> Iterator[Session]`
  - ORM models (all `id` columns bigint identity):
    - `Source(id, slug unique, name, area, access ['api'|'url'|'manual'], cron str default '0 3 * * *', enabled bool, config jsonb default {}, created_at)`
    - `Indicator(id, source_id, code, name, unit, description; unique(source_id, code))`
    - `DataPoint(id, source_id, key_hash char(40), dataset, indicator, country, country_iso3 nullable, period, dims jsonb, value_num numeric nullable, value_text text nullable, value_hash char(40), first_seen_at, last_changed_at; unique(source_id, key_hash); index(source_id, indicator))`
    - `Run(id, source_id, status, started_at, finished_at, vintage nullable, rows_total, added, altered, removed, error text, notify_error text, raw_path text, trigger ['schedule'|'manual'|'approve'])`
    - `Change(id, run_id, source_id, key_hash, change_type, dataset, indicator, country, period, dims, old_num, old_text, new_num, new_text, created_at; index(source_id, indicator, created_at); index(run_id))`
    - `Upload(id, source_id, who text, filename, created_at, run_id nullable)`. The table exists for Plan 2.
  - Test fixture `db_session` in `tests/conftest.py`: creates the schema via Alembic `upgrade head` once per session on `TEST_DATABASE_URL` and wraps each test in a rolled-back transaction.

- [ ] **Step 1: Scaffold.** Use `uv init` style `pyproject.toml` with the dependencies from Tech Stack and a `[project.optional-dependencies] dev` group (pytest, respx). Write `docker-compose.yml` with `db` (postgres:16, volume `pgdata`), `api` (`uvicorn app.api.main:app`, port 8000) and `worker` (`python -m app.worker`), all sharing `.env` plus a `raw` volume mounted at `RAW_DIR=/data/raw`. `.gitignore` covers `.env`, `__pycache__` and `.venv`.
- [ ] **Step 2: Write the failing test** `tests/test_schema.py::test_upgrade_creates_tables`. It asserts that `inspect(engine).get_table_names()` includes `sources, indicators, data_points, runs, changes, uploads`, and that `data_points` has the unique constraint on `(source_id, key_hash)`.
- [ ] **Step 3: Run it.** Command: `cd backend && docker compose up -d db && TEST_DATABASE_URL=postgresql+psycopg://kpi:kpi@localhost:5432/kpi_test uv run pytest tests/test_schema.py -v`. Expected: FAIL, because there is no migration yet.
- [ ] **Step 4: Implement** `config.py`, `db.py`, `models.py` and migration `0001_initial.py` (autogenerate, then review).
- [ ] **Step 5: Run it again.** Same command. Expected: PASS.
- [ ] **Step 6: Commit.** `git add -A && git commit -m "feat: scaffold backend, settings and schema"`

---

### Task 2: Canonical rows and value normalisation

**Files:**
- Create: `backend/app/core/canonical.py`, `backend/tests/test_canonical.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) RawRow(dataset: str, indicator: str, country: str, period: str, value: Any, dims: Mapping[str, str] = {})`
  - `@dataclass(frozen=True) CanonicalRow(dataset, indicator, country, period, dims_json: str, key_hash: str, value_num: Decimal | None, value_text: str | None, value_hash: str)`
  - `MISSING_MARKERS = {"", "..", "...", "n/a", "na", "nan", "-", "—", "none", "null"}` (case-insensitive, trimmed)
  - `canonicalize_value(v: Any) -> tuple[Decimal | None, str | None] | None`. Returns `None` for missing values. Numbers become a `Decimal` rounded to 10 significant digits, normalised with no trailing zeros. Text that doesn't parse as a number is trimmed and stored as text.
  - `to_canonical(row: RawRow) -> CanonicalRow | None`. Returns `None` when the value is missing. `key_hash` is the sha1 of `"\x1f".join([dataset, indicator, country, period, dims_json])`, where `dims_json = json.dumps(dims, sort_keys=True, separators=(",", ":"))`. `value_hash` is the sha1 of `"n:" + str(value_num)` or `"t:" + value_text`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_formatting_noise_is_equal():
    a, b, c = (to_canonical(RawRow("d","i","FRA","2024",v)) for v in ("1.50", 1.5, 1.5000000001))
    assert a.value_hash == b.value_hash == c.value_hash

def test_missing_markers_are_absent():
    for v in ("..", "", "  n/a ", float("nan"), None, "NaN"):
        assert to_canonical(RawRow("d","i","FRA","2024",v)) is None

def test_text_values_kept():
    r = to_canonical(RawRow("d","status","FRA","2024"," Free "))
    assert r.value_text == "Free" and r.value_num is None

def test_dims_order_does_not_change_key():
    a = to_canonical(RawRow("d","i","FRA","2024",1,{"sex":"F","age":"15-24"}))
    b = to_canonical(RawRow("d","i","FRA","2024",1,{"age":"15-24","sex":"F"}))
    assert a.key_hash == b.key_hash

def test_thousands_separator_and_negative():
    assert to_canonical(RawRow("d","i","FRA","2024","-1,234.5")).value_num == Decimal("-1234.5")
```

- [ ] **Step 2: Run it.** Command: `uv run pytest tests/test_canonical.py -v`. Expected: FAIL (ImportError).
- [ ] **Step 3: Implement** `canonical.py`.
- [ ] **Step 4: Run it again.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat: canonical rows and value normalisation`

---

### Task 3: Set-based diff and apply

**Files:**
- Create: `backend/app/core/diff.py`, `backend/tests/test_diff.py`

**Interfaces:**
- Consumes: `CanonicalRow`, ORM models.
- Produces:
  - `class DuplicateKeyError(Exception)`. Its message lists up to 5 conflicting keys as `dataset/indicator/country/period dims`.
  - `@dataclass DiffResult(existing: int, total: int, added: int, altered: int, removed: int)`
  - `stage_rows(session, rows: Iterable[CanonicalRow]) -> int`. Creates `TEMP TABLE staging (... ) ON COMMIT DROP`, bulk-loads it with psycopg `COPY`, collapses identical duplicates, and raises `DuplicateKeyError` on conflicting duplicates. Returns the distinct row count.
  - `compute_diff(session, source_id: int, complete: bool) -> DiffResult`. Counts only, with no writes. `removed` is 0 when `complete` is False.
  - `apply_diff(session, source_id: int, run_id: int, complete: bool, record_changes: bool) -> None`. Inserts `changes` rows (when `record_changes`) for added, altered and, if complete, removed rows. Upserts `data_points`, setting `last_changed_at = now()` only on added and altered rows, and deletes removed rows. All of this is SQL `INSERT … SELECT` / `UPDATE … FROM` / `DELETE … USING` against `staging`, never Python loops.

- [ ] **Step 1: Write the failing tests.** Each test seeds `data_points` through a baseline `apply_diff(..., record_changes=False)` call.

```python
def test_added_altered_removed(db_session, source):
    seed(db_session, source, [row("FRA","2023",1), row("FRA","2024",2), row("DEU","2024",3)])
    stage_rows(db_session, [row("FRA","2023",1), row("FRA","2024",2.5), row("ITA","2024",4)])
    assert compute_diff(db_session, source.id, complete=True) == DiffResult(existing=3, total=3, added=1, altered=1, removed=1)

def test_incomplete_fetch_never_removes(db_session, source):
    seed(db_session, source, [row("FRA","2023",1), row("DEU","2024",3)])
    stage_rows(db_session, [row("FRA","2023",1)])
    assert compute_diff(db_session, source.id, complete=False).removed == 0

def test_apply_writes_changes_with_old_and_new(db_session, source, run):
    seed(db_session, source, [row("FRA","2024",2)])
    stage_rows(db_session, [row("FRA","2024",2.5)])
    apply_diff(db_session, source.id, run.id, complete=True, record_changes=True)
    ch = db_session.scalars(select(Change)).one()
    assert (ch.change_type, ch.old_num, ch.new_num) == ("altered", Decimal("2"), Decimal("2.5"))

def test_identical_duplicates_collapse(db_session, source):
    assert stage_rows(db_session, [row("FRA","2024",2), row("FRA","2024","2.0")]) == 1

def test_conflicting_duplicates_raise(db_session, source):
    with pytest.raises(DuplicateKeyError, match="FRA/2024"):
        stage_rows(db_session, [row("FRA","2024",2), row("FRA","2024",3)])

def test_value_becoming_missing_is_removed_not_altered(db_session, source):
    seed(db_session, source, [row("FRA","2024",2)])
    stage_rows(db_session, [r for r in [to_canonical(RawRow("d","i","FRA","2024",".."))] if r])
    assert compute_diff(db_session, source.id, complete=True).removed == 1
```

`row(country, period, value)` is a test helper in `conftest.py` that returns `to_canonical(RawRow("d","i",country,period,value))`.

- [ ] **Step 2: Run them.** Command: `uv run pytest tests/test_diff.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `diff.py`.
- [ ] **Step 4: Run them again.** Expected: PASS. Also add `test_200k_rows_under_30s`, marked `@pytest.mark.slow`, which stages 200,000 generated rows and asserts that diff plus apply finishes in under 30 s.
- [ ] **Step 5: Commit.** `feat: set-based diff and apply`

---

### Task 4: Connector contract, raw store, pipeline

**Files:**
- Create: `backend/app/connectors/base.py`, `backend/app/connectors/registry.py`, `backend/app/core/raw_store.py`, `backend/app/core/pipeline.py`, `backend/tests/test_pipeline.py`
- Modify: `backend/tests/conftest.py` (add `FakeConnector`)

**Interfaces:**
- Consumes: `to_canonical`, `stage_rows`, `compute_diff`, `apply_diff`, `DuplicateKeyError`, models.
- Produces:
  - `@dataclass IndicatorMeta(code: str, name: str, unit: str | None = None, description: str | None = None)`
  - `@dataclass ParseResult(rows: Iterable[RawRow], complete: bool, indicators: list[IndicatorMeta], vintage: str | None = None)`
  - `class Connector(Protocol)` with `slug: str; name: str; area: str; access: str`, plus `download(self, client: httpx.Client, dest: Path) -> None` (writes exactly one raw file at `dest`) and `parse(self, raw: Path) -> ParseResult`.
  - `registry.CONNECTORS: dict[str, Connector]`, `get_connector(slug) -> Connector`, and `sync_sources(session) -> None`, which upserts one `sources` row per registered connector without touching `cron` or `enabled` on existing rows.
  - `raw_store.path_for(slug: str, run_id: int, suffix: str) -> Path` and `prune(session, source_id: int, keep: int) -> None`, which deletes raw files of older runs but never those of `suspect_pending` runs.
  - `pipeline.run_source(session_factory, slug: str, trigger: str = "schedule", notifier: Notifier | None = None) -> Run`
  - `pipeline.approve_run(session_factory, run_id: int, notifier=None) -> Run` and `pipeline.reject_run(session_factory, run_id: int) -> Run`
  - `Notifier` Protocol: `notify_run(run: Run, source: Source, top_indicators: list[tuple[str, int]], streak: int) -> None` (the implementation is Task 5).

**`run_source` behaviour (the order matters):**
1. Create `Run(status='running')` and commit it in its own short transaction.
2. Download to `raw_store.path_for(...)`. On any exception, set `status='failed'` with `error=repr(e)`, notify, and return.
3. Parse, canonicalise, `stage_rows`, then `compute_diff`, all in one transaction. On any exception: roll back, mark the run `failed`, notify, return.
4. If the source has no `data_points` and no earlier `baseline` or `ok` run, call `apply_diff(record_changes=False)` and set `status='baseline'`.
5. Else if `existing > 0 and removed / existing > SUSPECT_REMOVAL_RATIO`, set `status='suspect_pending'` and apply nothing. Any earlier `suspect_pending` run of the source becomes `superseded`.
6. Else if `added + altered + removed == 0`, set `status='no_change'`.
7. Else call `apply_diff(record_changes=True)` and set `status='ok'`.
8. Upsert `indicators` from `ParseResult.indicators`, set the counts, `vintage` and `finished_at`, commit, call `prune`, then call the notifier. Notifier exceptions are caught and stored in `run.notify_error`.

`approve_run` is allowed only on the latest `suspect_pending` run of its source. It re-parses the run's raw file, applies the diff with `record_changes=True` and sets `status='ok'`. `reject_run` sets `status='rejected'`.

- [ ] **Step 1: Write the failing tests** using `FakeConnector(rows=[...], complete=True, fail_download=False)`:
  - `test_first_run_is_baseline_without_changes`: status is `baseline`, `data_points` has the rows, `changes` is empty, the notifier was called once.
  - `test_second_identical_run_is_no_change_and_silent`: status is `no_change`, and the notifier was called with the run, which the notifier itself skips (asserted in Task 5).
  - `test_changes_are_recorded`: status `ok`, counts `(1, 1, 0)`.
  - `test_download_failure_leaves_data_untouched`: `fail_download=True` gives status `failed`, the `data_points` count is unchanged, and `error` contains the exception text.
  - `test_parse_failure_rolls_back`: a fake connector that raises halfway through iterating its rows gives status `failed` and no partial `data_points`.
  - `test_mass_removal_is_held`: baseline of 10 rows, then 7 rows gives status `suspect_pending` and `data_points` still has 10.
  - `test_approve_applies_latest_pending`: approving gives status `ok` and `data_points` has 7. Approving a `superseded` run raises `ValueError`.
  - `test_notifier_error_is_recorded_not_raised`: the run is `ok` and `notify_error` is set.
- [ ] **Step 2: Run them.** Command: `uv run pytest tests/test_pipeline.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `base.py`, `registry.py`, `raw_store.py` and `pipeline.py`.
- [ ] **Step 4: Run them again.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat: connector contract and run pipeline with guards`

---

### Task 5: Mattermost notifier

**Files:**
- Create: `backend/app/notify/mattermost.py`, `backend/tests/test_mattermost.py`

**Interfaces:**
- Consumes: `Run`, `Source`, the `Notifier` protocol.
- Produces: `MattermostNotifier(webhook_url: str | None, dashboard_base_url: str, consecutive_failure_alert: int)`, which implements `notify_run`. Also `format_run_message(run, source, top_indicators, streak) -> str | None`, where `None` means send nothing. Sending is `httpx.post(webhook_url, json={"text": text}, timeout=10)`; a non-2xx response raises. When `webhook_url` is not set, nothing is sent and no error is raised.

Exact message formats (the run link is `{DASHBOARD_BASE_URL}/runs/{run.id}`):
- ok: `**{source.name}** — {added:,} rows added · {altered:,} values revised · {removed:,} removed` + (`\nRelease: {vintage}` if vintage) + `\nTop indicators: {name} ({n:,}), …` (max 5) + `\n[View changes]({link})`
- baseline: `**{source.name}** — baseline loaded: {rows_total:,} rows`
- failed: `:warning: **{source.name}** — check failed: {first line of error, max 200 chars}\n[View run]({link})`, plus `\nFailed {streak} times in a row.` when `streak >= consecutive_failure_alert`
- suspect_pending: `:warning: **{source.name}** — {removed:,} of {existing:,} rows would be removed. Changes held for review.\n[Review]({link})`
- no_change, rejected, superseded: `None`

`streak` is computed by the pipeline: the number of consecutive `failed` runs, counting back from the latest.

- [ ] **Step 1: Write the failing tests**: one `format_run_message` test per status, asserting the exact strings above; `test_no_change_sends_nothing` (respx route not called); `test_missing_webhook_is_noop`; `test_http_500_raises` (so the pipeline records it in `notify_error`).
- [ ] **Step 2: Run them.** Command: `uv run pytest tests/test_mattermost.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `mattermost.py`. Top indicators come from `changes` grouped by indicator for the run; the pipeline passes them in.
- [ ] **Step 4: Run them again.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat: Mattermost run notifications`

---

### Task 6: World Bank WDI connector

**Files:**
- Create: `backend/app/connectors/worldbank_wdi.py`, `backend/tests/fixtures/wdi/WDI_CSV_sample.zip`, `backend/tests/test_worldbank_wdi.py`
- Modify: `backend/app/connectors/registry.py`

**Interfaces:**
- Consumes: the `Connector` protocol, `RawRow`, `IndicatorMeta`.
- Produces: `WorldBankWDI` with `slug="worldbank-wdi"`, `area="economic"`, `access="api"`. It downloads the WDI bulk CSV zip (expected at `https://databank.worldbank.org/data/download/WDI_CSV.zip`; verify the URL and record it in the docstring). `parse` reads the data CSV from the zip with pandas in chunks and melts year columns into rows `RawRow(dataset="WDI", indicator=<Indicator Code>, country=<Country Code>, period=<year>, value=...)`. Indicators come from the series CSV (`Series Code`, `Indicator Name`, `Unit of measure` if present). The result has `complete=True` only if the zip passes `ZipFile.testzip()` and the data CSV has every expected column.

- [ ] **Step 1: Build the fixture.** Download the real zip once, keep 3 countries × 5 indicators × all years, and re-zip it with the same file names.
- [ ] **Step 2: Write the failing tests:**
  - `test_parse_counts_and_keys`: the row count equals the number of non-missing cells in the fixture, and a known cell (for example `NY.GDP.MKTP.CD`, `FRA`, `2020`) has the value from the file.
  - `test_indicator_metadata`: the parsed indicators include `NY.GDP.MKTP.CD` with a name.
  - `test_truncated_zip_fails`: parsing a truncated copy of the fixture raises.
- [ ] **Step 3: Run them.** Command: `uv run pytest tests/test_worldbank_wdi.py -v`. Expected: FAIL.
- [ ] **Step 4: Implement** the connector and register it.
- [ ] **Step 5: Run them again.** Expected: PASS. Then run it for real: `uv run python -m app.cli run worldbank-wdi` against the dev database. Expected: status `baseline` with more than 1,000,000 rows.
- [ ] **Step 6: Commit.** `feat: World Bank WDI connector`

---

### Task 7: IMF WEO connector

**Files:**
- Create: `backend/app/connectors/imf_weo.py`, `backend/tests/fixtures/imf/`, `backend/tests/test_imf_weo.py`
- Modify: `registry.py`

**Interfaces:**
- Produces: `ImfWeo` with `slug="imf-weo"`, `area="economic"`, `access="api"`. `download` uses the IMF DataMapper API (expected `https://www.imf.org/external/datamapper/api/v1/indicators` and `/v1/{indicator}`; verify and record). It fetches the indicator list, then each indicator, at most 2 requests per second with 3 retries using backoff, and writes one JSON file `{"indicators": {...}, "data": {code: payload}}`. If any indicator still fails after the retries, the file records it under `"failed"` and `parse` returns `complete=False`. `parse` emits `RawRow(dataset="WEO", indicator=code, country=iso3, period=year, value=v)`. `vintage` is the release label the API reports, or `None` if it reports none; record which in the docstring.

- [ ] **Step 1: Build the fixture.** Save real responses for 3 indicators and one indicator-list response. Also make a fixture where one indicator is marked as failed.
- [ ] **Step 2: Write the failing tests:**
  - `test_parse_rows`: a known value from the saved response is present.
  - `test_failed_indicator_marks_incomplete`: `complete is False`.
  - `test_download_retries_then_records_failure`: respx returns 500 three times for one indicator, and the written file has it under `"failed"`.
- [ ] **Step 3: Run them.** Command: `uv run pytest tests/test_imf_weo.py -v`. Expected: FAIL.
- [ ] **Step 4: Implement** the connector and register it.
- [ ] **Step 5: Run them again.** Expected: PASS. Then run it for real with `python -m app.cli run imf-weo`. Expected: `baseline`.
- [ ] **Step 6: Commit.** `feat: IMF WEO connector`

---

### Task 8: SIPRI military expenditure connector

**Files:**
- Create: `backend/app/connectors/sipri_milex.py`, `backend/tests/fixtures/sipri/milex_sample.xlsx`, `backend/tests/test_sipri_milex.py`
- Modify: `registry.py`

**Interfaces:**
- Produces: `SipriMilex` with `slug="sipri-milex"`, `area="military"`, `access="url"`. It downloads the SIPRI Military Expenditure Excel file from its public URL. Find the current link on sipri.org/databases/milex and check the terms of use; if they forbid automated download, stop and report back instead of writing the connector. `parse` reads each data sheet (for example "Constant (…) US$", "Current US$", "Share of GDP", "Per capita"): the sheet becomes the indicator code (slugified) and the sheet title becomes the indicator name. The header row is found by locating the cell `Country`. Rows without a country are skipped, and so are region subtotal rows (rows whose name is in the file's region list). Country names are kept as `country`; ISO3 mapping is Plan 3. `complete=True` when every expected sheet is found.

- [ ] **Step 1: Build the fixture.** Trim the real file to 3 countries per sheet, keeping the original header rows and notes.
- [ ] **Step 2: Write the failing tests:**
  - `test_parse_all_sheets`: every expected sheet becomes an indicator.
  - `test_known_value`: a value read from the file.
  - `test_missing_sheet_marks_incomplete`.
  - `test_notes_rows_ignored`: no rows from the footnotes.
- [ ] **Step 3: Run them.** Command: `uv run pytest tests/test_sipri_milex.py -v`. Expected: FAIL.
- [ ] **Step 4: Implement** the connector and register it.
- [ ] **Step 5: Run them again.** Expected: PASS. Then run it for real. Expected: `baseline`.
- [ ] **Step 6: Commit.** `feat: SIPRI military expenditure connector`

---

### Task 9: Scheduler worker and CLI

**Files:**
- Create: `backend/app/worker.py`, `backend/app/cli.py`, `backend/tests/test_worker.py`

**Interfaces:**
- Consumes: `run_source`, `sync_sources`, `MattermostNotifier`.
- Produces:
  - `build_scheduler(session_factory, notifier) -> BackgroundScheduler`: one cron job per enabled source, id `source:{slug}`, from `Source.cron`, with `max_instances=1` and `coalesce=True`.
  - `reload_jobs(scheduler, session_factory) -> None`, called every 60 s, picks up changes to `cron` and `enabled`.
  - Manual runs: the API (Task 10) writes `Run(status='queued', trigger='manual')`, and the worker polls every 15 s for queued runs and executes them. `run_source` gains an optional `run_id` parameter so it reuses the queued row. Add `queued` to the run statuses.
  - CLI: `python -m app.cli sync-sources` and `python -m app.cli run <slug>`, which prints the run status and counts.

- [ ] **Step 1: Write the failing tests:**
  - `test_jobs_match_enabled_sources`: 3 sources with one disabled give 2 jobs with the right cron triggers.
  - `test_reload_picks_up_cron_change`.
  - `test_queued_run_is_executed`: a queued run plus one poll tick reaches a final status using the fake connector.
- [ ] **Step 2: Run them.** Command: `uv run pytest tests/test_worker.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `worker.py` and `cli.py`. The `__main__` of `worker.py` calls `sync_sources`, starts the scheduler and blocks.
- [ ] **Step 4: Run them again.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat: scheduler worker and CLI`

---

### Task 10: REST API with shared-password auth

**Files:**
- Create: `backend/app/api/main.py`, `backend/app/api/routes_sources.py`, `backend/app/api/routes_runs.py`, `backend/tests/test_api.py`

**Interfaces:**
- Consumes: models, `approve_run`, `reject_run`.
- Produces (all under `/api` and all requiring HTTP Basic, where any username plus `DASHBOARD_PASSWORD` is accepted, compared with `secrets.compare_digest`; `/api/health` is the only public route):
  - `GET /api/health` → `{"status":"ok"}`
  - `GET /api/sources` → `[{slug, name, area, access, enabled, cron, last_run: {id, status, finished_at, added, altered, removed} | null, last_change_at, rows_tracked, changes_30d, failing_streak}]`
  - `PATCH /api/sources/{slug}` with body `{enabled?: bool, cron?: str}`. A cron string APScheduler can't parse returns 422.
  - `POST /api/sources/{slug}/run` → 202 `{run_id}`. Creates a queued run. Returns 409 if the source already has a run that is `queued` or `running`.
  - `GET /api/runs?source=&status=&since=&page=1&page_size=50` → `{items: [...], total}`, newest first. `page_size` is at most 200.
  - `GET /api/runs/{id}` → the run plus `top_indicators`
  - `GET /api/runs/{id}/changes?change_type=&indicator=&country=&page=&page_size=` → `{items: [{change_type, dataset, indicator, country, period, dims, old, new}], total}`. For a `suspect_pending` run, returns the would-be changes computed from its raw file, with `"pending": true`.
  - `POST /api/runs/{id}/approve` and `POST /api/runs/{id}/reject`. An invalid state returns 409.
  - Paginated responses are server-side, with `ORDER BY` plus `LIMIT/OFFSET` on indexed columns.

- [ ] **Step 1: Write the failing tests** using FastAPI `TestClient` with the `db_session` override:
  - `test_requires_password`: 401 without credentials, 401 with a wrong password, 200 with the right one.
  - `test_health_is_public`.
  - `test_sources_list_shape`: keys exactly as listed above.
  - `test_patch_invalid_cron_422`.
  - `test_run_now_creates_queued_and_409_on_double`.
  - `test_runs_pagination_and_filter`: 120 runs, `page_size=50` → 50 items, `total=120`, and filtering by `status=failed` works.
  - `test_changes_filter_by_country`.
  - `test_approve_reject_state_errors`: approving an `ok` run returns 409.
- [ ] **Step 2: Run them.** Command: `uv run pytest tests/test_api.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the routes.
- [ ] **Step 4: Run them again.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat: REST API with shared-password auth`

---

### Task 11: End-to-end check in Docker Compose

**Files:**
- Create: `README.md` (setup, env vars, commands)
- Modify: `docker-compose.yml` (healthchecks; `api` and `worker` depend on `db` being healthy and run `alembic upgrade head` at start)

- [ ] **Step 1:** Run `cp .env.example .env`, set `DASHBOARD_PASSWORD`, and leave `MATTERMOST_WEBHOOK_URL` empty or point it at a test channel. Then run `docker compose up -d --build`. Expected: all services report healthy (`docker compose ps`).
- [ ] **Step 2:** Run `docker compose exec worker python -m app.cli run sipri-milex` twice. Expected: the first run prints `baseline`, the second `no_change`.
- [ ] **Step 3:** Run `curl -u x:$DASHBOARD_PASSWORD localhost:8000/api/sources`. Expected: 3 sources, and `sipri-milex` has a last run.
- [ ] **Step 4:** Edit one value in the raw SIPRI file, then reprocess it with `python -m app.cli run sipri-milex --from-raw <path>` (add this flag to the CLI, test-only, documented). Expected: status `ok` with `altered=1`, and a Mattermost message if the webhook is set.
- [ ] **Step 5:** Run the full suite: `uv run pytest -m "not slow"`. Expected: all pass.
- [ ] **Step 6: Commit.** `chore: compose healthchecks, README, e2e check`
