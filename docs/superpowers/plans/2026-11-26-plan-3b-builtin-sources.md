# Plan 3b: Remaining Built-in Sources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add connectors for the sources in spec §3 that are not built yet. Licensed or keyed ones run only with the user's own key, per the user's decision on 2026-11-26.

**Architecture:**
- Each source is one module in `backend/app/connectors/` that implements the existing `Connector` protocol (`download(client, dest)` writes one raw file; `parse(raw) -> ParseResult`). It is registered in `CONNECTORS` and gets an attribution line.
- Large portals track a curated list of country-level series. The list lives in `Source.config["series"]`, so the user can edit it from Settings (Task 1).
- Keyed sources declare `credential_hosts` and use the key mechanism from §5.10.

**Tech Stack:** Python 3.13, httpx, pandas, openpyxl, FastAPI, Vue 3.

**Spec:** docs/superpowers/specs/2026-11-24-kpi-monitor-design.md (§3 sources and tracking depth, §5.2 ACLED/UCDP aggregation, §5.10 keys).

## Global Constraints

- **Verify before writing.** Every connector is checked against the live source first: endpoint, auth, licence or terms, format and update cadence. The facts go in the module docstring with the date, following `worldbank_wdi.py`.
- **Terms first.** If a source's terms forbid automated access, do not scrape. Mark it `access='manual'` with no connector, add a README note, and report it.
- **Keys:**
  - A keyed source with no key set fails its run with the clear error `"needs a key: add one in Settings → Keys and pick it for this source"`. It never sends an anonymous request that could breach the terms.
  - Never put the raw text of an httpx exception, a request URL or a response repr into logs, errors or the raw file (see the `base.py` contract).
- **Rows:**
  - Canonical rows are country-level. Use the ISO3 code where the source provides it, and keep aggregates as-is.
  - Values go through the existing canonicalisation, so there is no formatting noise.
  - Parsing a fixture must be deterministic.
- **Tests:** they use small recorded fixtures in `backend/tests/fixtures/<slug>/`, a few KB each, trimmed from real responses. Tests never touch the network.
- **Network failure:** a live check that cannot reach the host (proxy refusal) gets a BLOCKED report for that connector, never invented formats.
- **Labels:** `area` is one of economic, military, social, cultural or political, matching the feed filter.
- **Never touch the dev DB `kpi`.**
- **Areas** are free text and the change-feed filter reads them from the sources (Plan 3a), so any of the five labels works without code changes.
- **Sources that already hold a key** (Plan 3a final review, M9): when a connector starts declaring `credential_hosts`, any source that already holds a key gets `needs_baseline=true` and `needs_baseline_at=clock_timestamp()` on its first run. The rule: the source has a `credential_id` and no previous keyed run. The point is that keyed data is never diffed against data fetched without the key. This lives in Task 1's shared helper.
- **Every POST/PATCH/DELETE** to the API, in tests and in scripts, sends `X-Requested-With: kpi-monitor`.

## Review Focus

1. **The source changes its format** (renamed column, extra sheet, HTML error page with status 200). The parse fails with a clear error, and `complete=False` or a failed run. It never reports "everything removed".
2. **Partial or paginated API responses** (a page fails mid-way). The run is incomplete or failed, never a silent partial set that reads as removals.
3. **A key that is missing, wrong or expired** (401/403). Map 401/403 to the existing `key_rejected` path, and show no secret in the error.
4. **A huge download** (many MB). Stream it to disk; never hold the whole response in memory.
5. **The curated series list is edited to an unknown series id.** That series fails with a warning and an incomplete run, not a failed source.

---

### Task 1: Curated series list for the portal sources

**Files:**
- Create: `backend/app/connectors/curated.py` (helpers)
- Modify: `backend/app/api/routes_sources.py` (PATCH `config.series`)
- Modify: `frontend/src/pages/settings/BuiltinSources.vue` (an editable list)
- Test: `backend/tests/test_curated.py`, plus a vitest

**Interfaces:**
- Produces:
  - `curated_series(session_or_config, default: list[str]) -> list[str]`: returns `config["series"]` if set, else the connector's `DEFAULT_SERIES`.
  - `PATCH /api/sources/{slug}` accepts `{"series": ["id", ...]}`, but only for connectors that declare `DEFAULT_SERIES`. It returns 422 otherwise. Each id must be 1–200 printable ASCII characters, with at most 500 ids.
  - Adding a series makes its first fetch a silent baseline: put it in `baseline_indicators` when it was not in the previous run's set. Track the last set in `config["series_seen"]`.
  - Removing a series deletes its data_points silently, the same rule as a disabled KPI (Plan 2 R8).
- [ ] **Steps:** test first, implement, then the UI. A "Tracked series" textarea, one id per line, shown only for sources that support it, with "Reset to defaults". Commit `feat(sources): editable curated series list`.

### Task 2: SIPRI Arms Transfers (military)

- Verify the trade-register CSV or RTF export at armstrade.sipri.org, and whether it can be fetched by a plain GET.
- If it cannot be automated (a form POST that needs a session, or terms that forbid it), make the source `manual` and report it.
- Rows: indicator `tiv_imports` and `tiv_exports` per country per year, from the TIV tables.
- Licence: the same SIPRI terms as milex, so reuse the attribution style.
- Fixture tests: parse, a format change, and an empty file.

### Task 3: UCDP and ACLED (military/conflict, aggregated per spec §5.2)

- **UCDP:** check whether the GED API (ucdpapi.pcr.uu.se) needs a token now. If it does, it is keyed (`x-ucdp-access-token` header).
  - Aggregate events and fatalities per country per year, as §5.2 specifies.
  - Paginate. Any page failure fails the run.
- **ACLED:** keyed with the user's own key, as the API requires. Verify the current auth (key plus email, or OAuth) from the official docs. If the auth needs more than one value, take the key from the credential and the email from `Source.config["email"]`, editable in Settings.
  - Aggregate events and fatalities per country per year (or month, if §5.2 says so).
- Tests: fixture aggregation, pagination failure, and 401 → key_rejected.

### Task 4: FRED and UNDP HDR (economic/social)

- **FRED:** keyed (`api_key` query parameter, host api.stlouisfed.org). Use a curated default of about 30 country-level series, for example GDP and CPI for G20. Use observations with `realtime` defaults.
- **UNDP HDR:** use the composite indices CSV download. Check whether the HDR API key is needed or the public CSV is enough, and prefer the public CSV if it is. Indicators: HDI, GDI, GII, MPI components, per country per year.
- Tests: fixtures, a missing key gives the "needs a key" failure, and a curated list edit works.

### Task 5: OECD and UN Data / UNCTADstat (SDMX portals, curated)

- Use the OECD SDMX REST API (sdmx.oecd.org) and the UNCTADstat bulk or API. Verify both endpoints.
- Use a curated default of about 20 datasets each, country-level, covering economic, trade and population.
- Use one SDMX-CSV parser shared in `curated.py` or `sdmx.py`.
- Tests: fixture SDMX-CSV, and an unknown dataflow id gives a warning and an incomplete run.

### Task 6: WHO GHO and UNESCO UIS (social/cultural)

- **WHO GHO:** OData API (ghoapi.azureedge.net, or its current replacement; verify). Use a curated default of about 30 indicator codes, for example life expectancy and UHC index.
- **UNESCO UIS:** use the current UIS data API or the bulk download service (verify; the old SDMX API was retired). Use a curated default for education and culture indicators.
- Tests: fixtures, OData paging, and an HTML error with status 200 makes the parse fail.

### Task 7: Our World in Data (mixed, curated)

- Use the per-chart CSV (`https://ourworldindata.org/grapher/<slug>.csv`) or the GitHub catalog (verify the current method and the CC BY licence).
- Use a curated default of about 25 chart slugs. Use the Code column as ISO3 and drop the OWID_ aggregates unless they are meaningful.
- Tests: fixtures and an unknown slug.

### Task 8: World Happiness, Freedom House and V-Dem (downloads)

- **World Happiness Report:** the yearly Excel figure data. Verify the URL, which changes each year: discover it from the data page link pattern, or use a configurable `config["url"]` with the current default.
- **Freedom House:** the "All data FIW" Excel. Check the terms for automated download; if they forbid it, make it manual.
- **V-Dem:** the country-year dataset needs a registration form. If so, make it manual upload with a parser for the CSV the user downloads, or use a key or login if the user has one (§5.11 logins can cover a web login).
- Tests: fixture parse for each source, and sheet or column renames.

### Task 9: Docs, register and end-to-end

- README "Data and licensing" gets one line per new source: licence, key needed (yes/no), manual (yes/no) and attribution.
- Run `app.cli run <slug>` live once for each non-keyed connector against `kpi_e2e`. Record the row counts in the report, or BLOCKED with the reason.
- Run the full suites.
- Commit `docs: remaining sources`.
