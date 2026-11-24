# KPI Monitor — Design

Date: 2026-11-24
Status: approved 2026-11-24

## 1. Intent

Many public datasets publish country-level indicators (economic, military, cultural). They grow by adding a row per country (usually per country per year), and they also revise past values. The user wants one tool that:

- pulls every source on a schedule,
- detects every row that was **added** or **altered** (and, by our call, **removed**),
- shows the current data, the change history and **how often each KPI changes** on a Vue dashboard,
- posts a notification to Mattermost when a source changes.

Success: the user learns that a source changed without visiting 17 websites, and can see exactly what changed and how often each KPI tends to change.

## 2. Goals, non-goals, anti-goals

**Goals**
- Track **all** data of every listed source (Option A: track everything, filter on the dashboard).
- Detect added / altered / removed data points with old → new values.
- One Mattermost summary message per source per run that found changes.
- Dashboard: source overview, change feed, drill-down by source / indicator / country / year, and per-KPI change frequency.
- Excel-only sources: automatic download where a stable URL exists; manual upload through the dashboard otherwise.

**Non-goals (v1)**
- Analysis, forecasting, cross-source comparison or merging of "the same" indicator from different sources.
- A Mattermost bot that answers commands (webhook-only posting in v1; bot account later).
- Individual user accounts / roles. The tool runs on an **internal network**; access is protected by a single shared password (e.g. HTTP basic auth or a simple login), configured via environment variable. `uploads.who` records a free-text name entered at upload.

**Anti-goals (failure even if it technically works)**
- Flooding Mattermost: one message per changed data point. A WEO release revises thousands of values; this must be one message.
- Silent failures: a source that breaks (API change, URL moved) must be visible on the dashboard and in Mattermost, not just quietly stop producing changes.
- False "altered" alerts from formatting noise (e.g. `1.50` vs `1.5`, float rounding, re-ordered columns).
- Violating a source's terms of use by scraping.

## 3. Sources

All are in scope. Access methods are from prior knowledge; **the builder must verify each one** (endpoint, auth, licence, update cadence) before writing its connector.

| # | Source | Area | Access (to verify) |
|---|---|---|---|
| 1 | IMF World Economic Outlook / DataMapper (+ IMF SDMX data portal) | Economic | API (JSON / SDMX), no key |
| 2 | World Bank WDI | Economic / military / social | API, no key; bulk CSV zip also available |
| 3 | OECD | Economic | SDMX API |
| 4 | UN Data / UNCTADstat | Economic / trade / population | SDMX API + downloads |
| 5 | FRED | Economic | API, free key |
| 6 | TheGlobalEconomy.com | All areas | No public API → **manual Excel upload** |
| 7 | SIPRI Military Expenditure | Military | Excel at a fixed URL → automatic download |
| 8 | SIPRI Arms Transfers | Military | CSV export (verify whether automatable) |
| 9 | UCDP | Military / conflict | API (verify token requirement) |
| 10 | ACLED | Military / conflict | API, registration key |
| 11 | Global Firepower | Military | No API; ToS likely forbids scraping → **manual upload** |
| 12 | UNESCO UIS | Cultural / education | API |
| 13 | UNDP Human Development Reports | Social | API (key) + CSV/Excel |
| 14 | WHO Global Health Observatory | Social / health | OData API |
| 15 | Our World in Data | Mixed | CSV per chart / GitHub |
| 16 | World Happiness Report | Social | Excel download |
| 17 | Freedom House, V-Dem | Political / social | Excel / CSV downloads |

If a source's terms forbid automated access, it falls back to manual upload; the builder flags it rather than scraping.

**Tracking depth (decided with user):**
- **Tracked in full:** IMF WEO, World Bank WDI, SIPRI (both), UNDP HDI, Freedom House, V-Dem, World Happiness, UNESCO UIS, ACLED/UCDP (aggregated, §5.2), manual uploads.
- **Large portals (FRED, OECD, UN Data/UNCTAD, WHO GHO, Our World in Data):** track a curated list of **country-level** datasets/series. The builder compiles a sensible starting list per portal (economic, military, social/cultural coverage); users can add/remove datasets from the dashboard (Sources settings page) without code changes.
- **Manual sources (TheGlobalEconomy.com, Global Firepower):** no sample files yet. v1 builds the generic upload flow (§5.7) and parser interface; the per-source parsers are added once the user provides a real export of each. Not part of v1 acceptance.

## 4. Approaches considered

**Scope of tracking**
- **A. Track everything, filter on dashboard — chosen.** Never miss anything, no watchlist to maintain. Costs a real database and a few GB.
- B. Track a chosen indicator list — lighter, but blind outside the list.
- Hybrid (everything + watchlist with detailed alerts) — deferred; can be added later on top of A.

**Back end**
- **1. Python (FastAPI + Postgres) — chosen.** Best libraries for these formats (`sdmx1` for IMF/OECD/UN/Eurostat, `pandas`/`openpyxl` for Excel); large diffs are routine in pandas + Postgres.
- 2. Node/TypeScript (NestJS) — one language end to end, but weaker data libraries, more hand-written parsing.
- 3. Java (Spring Boot) — robust scheduling, but most code and slowest to add sources.

**Fixed choices (decided):** Vue 3 front end, Postgres, Docker Compose, Mattermost incoming webhook.

## 5. Design

### 5.1 Components (Docker Compose)

- `db` — Postgres.
- `api` — FastAPI: REST API for the dashboard, file-upload endpoint, manual "run now" trigger.
- `worker` — scheduler + connectors: runs each source on its schedule, normalizes, diffs, writes changes, sends notifications. Same Python codebase as `api`, different entrypoint.
- `web` — Vue 3 SPA (built static files served by nginx, proxying `/api` to `api`).

### 5.2 Data model (one canonical shape)

Every source is normalized to data points keyed by:

`(source, dataset, indicator, country, period, [extra dims]) → value`

- `period` is a string (`2024`, `2024-Q3`, `2024-05`) since sources differ in frequency.
- `extra dims` covers sources with more dimensions (sex, age group, unit). Stored as a normalized, sorted JSON of dimension → code, included in the key.
- **Forecast vintage is NOT part of the key** (our call). For IMF WEO and similar, we track the latest published value of each (indicator, country, year); a new release shows as revisions (altered) plus new years (added). The release/vintage label is recorded on the run.
- **Event-level sources (ACLED, UCDP)** are aggregated to country × month indicators (event count, fatalities, per event type) rather than storing raw events (our call). This fits the canonical model; a late-reported event shows as an altered monthly total.
- `country` keeps the source's own code plus a mapped ISO3 where possible (the builder chooses the mapping approach; unmapped regions/aggregates are kept, not dropped).
- `value` is stored as numeric where it parses, otherwise text (some sources have categorical values, e.g. Freedom House status).

Tables (indicative; builder may refine):

- `sources` — id, name, area, access type (api / url-download / manual), schedule, enabled, last run status.
- `indicators` — source, code, name, unit, description.
- `data_points` — the current value of every key, plus a value hash and `first_seen_at`, `last_changed_at`.
- `runs` — one row per fetch: source, started/finished, status (ok / no-change / failed / suspect-pending / rejected), counts of added / altered / removed, error text, raw-file reference.
- `changes` — one row per changed data point per run: run id, key, change type (added / altered / removed), old value, new value.
- `uploads` — manual files: who/when, source, filename, linked run.

### 5.3 Change detection

1. Connector downloads the full dataset (or, where the API supports it reliably, only what changed since last run — builder's call per source).
2. Normalize to canonical rows; canonicalize values (numeric parse, fixed precision rounding, trimmed text) so formatting noise doesn't count as a change.
3. Compare against `data_points` for that source by key:
   - key new → **added**
   - key exists, value hash differs → **altered** (store old and new)
   - key missing from a **complete** fetch → **removed**. Only emitted when the connector declares the fetch complete, so a partial/failed download never produces mass "removed" events.
4. Write `changes` and update `data_points` in one transaction per run.
5. Sanity guard: if a run would remove more than a threshold share of a source's rows (e.g. 20 %), mark the run **suspect — pending review**, don't apply it, and alert. Protects against a broken file or API outage. The dashboard's run detail shows the pending diff with **Approve & apply** / **Reject** buttons (our call); while a run is pending, later runs of that source are still fetched and compared but also held, and approving applies the latest one.
6. **First run** of a source is a baseline: load everything, record counts, do **not** emit per-row "added" changes or a big Mattermost message (send one "baseline loaded: N rows" note).

Keep the raw downloaded file for each run for a limited period (builder's call, e.g. last N runs per source) for debugging and re-processing.

### 5.4 Change frequency per KPI (user requirement)

For each indicator (and drillable per country), the dashboard shows:

- number of change events (added / altered / removed) over selectable windows (last 30 days, 12 months, all time),
- last changed date and first seen date,
- typical interval between updates (median gap between runs that changed it), e.g. "≈ every 6 months",
- a small timeline/sparkline of change counts over time.

Computed from the `changes` and `runs` tables; for performance, a rollup table or materialized view of change counts per (source, indicator, day) refreshed after each run.

Our interpretation: "frequency of changes" = how often the data of each KPI changes, as observed by this tool. History only starts when tracking starts.

### 5.5 Schedules

- Default: each source checked **daily**; configurable per source in the database / dashboard.
- Sources with known release calendars (IMF WEO April/October, SIPRI April) still get a daily check — cheap and catches surprise revisions.
- Large bulk downloads (World Bank, OECD) may run less often if needed; builder may tune.

### 5.6 Mattermost notifications

- Incoming webhook URL configured via environment variable. The dashboard's internal base URL is also an environment variable, used to build links in messages.
- One message per source per run **with changes**:
  `**IMF WEO** — 12 rows added · 840 values revised · 0 removed` + top indicators by change count + link to that run on the dashboard.
- Also posted: run failures, suspect runs (§5.3 step 5), and a source failing N runs in a row.
- No message for runs with no changes.

### 5.7 Manual uploads

- Dashboard page: choose source, upload `.xlsx`/`.csv`, see a preview of the parsed rows and diff counts, confirm to apply.
- Each manual source has a parser for its known file layout. If the layout doesn't match (columns renamed), the upload is rejected with a clear message rather than guessing.

### 5.8 Dashboard (Vue 3)

Pages:

1. **Overview** — card/table per source: status, last successful run, last change, rows tracked, changes in last 30 days; failed sources highlighted.
2. **Change feed** — chronological list of runs with changes; filter by source, area, change type, date.
3. **Run detail** — counts and a paginated table of changed data points (indicator, country, period, old → new).
4. **Explorer** — browse source → indicator → country; show the value series and its change history.
5. **KPI frequency** — table of indicators sortable by change count / last changed / typical interval, with sparklines (§5.4).
6. **Uploads** — manual file upload flow (§5.7).
7. **Sources settings** — enable/disable, schedule, "run now", and for large portals the list of tracked datasets/series (add/remove).

Tables must be server-side paginated and filtered (millions of rows).

**Colours:** use the user's own palette (to be supplied by the user). All colours live in one design-token file (CSS custom properties, light + dark) so the palette is dropped in without touching components; chart series colours come from the same tokens.

## 6. Constraints

- Data volume: millions of data points (WDI alone ≈ 1,400 indicators × 217 economies × 60+ years). Bulk inserts/upserts and set-based diffing in Postgres (e.g. load into a staging table and diff with SQL) rather than row-by-row Python.
- Some downloads take minutes; runs must not block the API.
- Respect each source's rate limits and terms; API keys (FRED, ACLED, UNDP, possibly UCDP) via environment variables, never in code.
- Runs on a single machine with Docker Compose.

## 7. Left to the builder

- Library choices beyond those named (ORM, scheduler, chart library, Vue UI kit).
- Per-source connector details, incremental vs full fetch, and verification of each access method in §3.
- Exact table schema, indexes, rollups, raw-file retention.
- Country code mapping approach.
- Value canonicalization rules (precision etc.), as long as formatting noise doesn't produce "altered".
- Order of building connectors (suggest: World Bank and IMF first, then one Excel download (SIPRI), one manual upload, then the rest).
- Testing approach (connectors tested against saved sample files).
