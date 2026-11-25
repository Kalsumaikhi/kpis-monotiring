# KPI Monitor

Tracks public statistical datasets, detects **added, altered and removed** values between releases, and posts a Mattermost alert when something changes. Plan 1 covers the data pipeline and alerts; Plan 2 adds the web dashboard.

## Sources

| Slug | Source | Notes |
|---|---|---|
| `worldbank-wdi` | World Bank World Development Indicators | about 280 MB download, roughly 9M rows |
| `imf-weo` | IMF World Economic Outlook | |
| `sipri-milex` | SIPRI Military Expenditure Database | about 1 MB workbook |

## Architecture

- **db**: PostgreSQL 16 (values, runs, change log).
- **api**: FastAPI app (`app.api.main:app`) on port 8000. It runs `alembic upgrade head` on start, so it is the only service that migrates.
- **worker**: APScheduler process (`python -m app.worker`). It runs each source on its schedule, polls for runs queued from the API, and sends Mattermost alerts. It starts once the api is healthy.

## Setup (Docker Compose)

```sh
cp .env.example .env
# edit .env: set DASHBOARD_PASSWORD and MATTERMOST_WEBHOOK_URL
docker compose up -d --build
docker compose ps          # db, api, worker should be healthy/running
curl localhost:8000/api/health
```

The db port is not published. For local debugging, add `ports: ["5432:5432"]` to the `db` service.

Note: the compose file was validated with `docker compose config` only. It was not run in the authoring environment, which has no Docker daemon. The pipeline was checked end to end against a local Postgres instead.

## Configuration (.env)

| Variable | Default | Meaning |
|---|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | `kpi` / `kpi` / `kpi` (in `.env.example`) | Database container credentials |
| `DATABASE_URL` | `postgresql+psycopg://kpi:kpi@localhost:5432/kpi` (`.env.example`: host `db`) | SQLAlchemy URL |
| `MATTERMOST_WEBHOOK_URL` | empty | Incoming webhook; empty disables alerts |
| `DASHBOARD_BASE_URL` | `http://localhost:8000` | Base URL used in alert links |
| `DASHBOARD_PASSWORD` | empty | Shared HTTP Basic password; empty or the placeholder `change-me` makes protected routes return 503 |
| `RAW_DIR` | `/data/raw` | Where downloaded raw files are kept |
| `RAW_RETENTION_RUNS` | `5` | Raw files kept per source |
| `SUSPECT_REMOVAL_RATIO` | `0.2` | Removal share above which a run is held as suspect |
| `CONSECUTIVE_FAILURE_ALERT` | `3` | Failed runs in a row before a failure alert |

## CLI

Run from `backend/` (or `docker compose exec worker ...`):

```sh
python -m app.cli sync-sources                       # register the connectors in the DB
python -m app.cli run <slug>                         # download, parse, diff, notify
python -m app.cli run <slug> --from-raw <path>       # test-only: reprocess a local raw file
python -m app.cli backfill-rollup                    # rebuild the change-frequency counts from all changes
python -m app.worker                                 # scheduler
```

`run` prints `slug: status rows=N added=N altered=N removed=N`. Exit codes: 1 failed, 2 bad arguments, 3 source busy. The first run of a source is a `baseline`; later runs report `no_change` or `ok`.

The KPI frequency and explorer views read per-day counts from `change_rollup`. Migration `0006` fills it from existing changes when an older database is upgraded; `backfill-rollup` rebuilds it by hand (safe to repeat).

## API

`GET /api/health` is public. All other routes need HTTP Basic auth (any username, password = `DASHBOARD_PASSWORD`).

- `GET /api/sources`, `PATCH /api/sources/{slug}`, `POST /api/sources/{slug}/run`
- `GET /api/runs`, `GET /api/runs/{id}`, `GET /api/runs/{id}/changes`
- `POST /api/runs/{id}/approve`, `POST /api/runs/{id}/reject`

## Tests

Needs a running Postgres and `TEST_DATABASE_URL` pointing at a database whose name contains `test`:

```sh
cd backend
TEST_DATABASE_URL=postgresql+psycopg://kpi:kpi@localhost:5432/kpi_test uv run pytest -m "not slow"
```

## Data and licensing

- **World Bank WDI**: CC BY 4.0. Attribute the World Bank.
- **SIPRI Military Expenditure Database**: free to download, cite and use for statistical or analytical purposes under its fair-use policy: non-commercial use and reproducing less than 10 per cent of the data set. Attribution is required ("Information from the Stockholm International Peace Research Institute (SIPRI) Military Expenditure Database, https://doi.org/10.55163/CQGC9685"). Commercial use, including internal tools for business advantage, needs a SIPRI licence. See the SIPRI terms and conditions.
- **IMF WEO**: see the IMF terms of use (imf.org copyright page) before redistributing the data.

## Dashboard

A Vue 3 single-page app in `frontend/`, served by nginx on <http://localhost:8080> (`docker compose up web`). nginx proxies `/api/` to the `api` service. Sign in with `DASHBOARD_PASSWORD` (any username); the password is kept in `sessionStorage` for the tab.

Local development:

```
cd frontend
npm install
npm run dev      # http://localhost:5173, proxies /api to http://localhost:8000
npx vitest run   # unit tests
npm run build    # type-check and build to dist/
```

All colours are CSS custom properties in `frontend/src/styles/tokens.css`, with light and dark themes.

### Pages

| Page | What it does |
|---|---|
| Overview | One card per source: last run, rows tracked, changes in 30 days, "Run now" |
| Changes | Run feed with filters; open a run for its change list (page changes show a `+`/`−` text excerpt) |
| Explorer | Source → indicator → country, with a chart, data points and change history |
| KPI frequency | How often each indicator changes (30 days, 12 months, all time) |
| Uploads | Manual uploads |
| Settings | Schedules for built-in sources, and custom sources (add, test, import, disable, delete) |

![Overview](docs/screenshots/overview-light.png)
![Overview, dark theme](docs/screenshots/overview-dark.png)
![Run detail of a page-change run](docs/screenshots/run-detail.png)
![KPI frequency](docs/screenshots/kpi-frequency.png)
![Explorer chart](docs/screenshots/explorer-chart.png)
![Settings, custom sources](docs/screenshots/settings-custom-sources.png)

## Custom sources

Besides the built-in datasets you can watch any web page or JSON API link (Settings → Custom sources). Each KPI is one link; KPIs are grouped under a source name.

- **Add KPI**: source name, KPI name, link. "Test link" shows what would be read. Under Advanced you can set a CSS selector (focus area), force page/API mode, or ask for a headless browser for JavaScript-built pages.
- **Import from Excel/CSV**: a `.xlsx` or `.csv` with the columns `Source name`, `KPI name`, `Link` and optionally `Key name` (see Keys below; the dialog offers a template). The preview marks each row as new, existing, duplicate (skipped) or invalid before anything is created.
- **Renaming a KPI** keeps its current value but starts its change history fresh: past changes stay in the change feed and run details under the old name, while the explorer and KPI frequency count only changes recorded after the rename.
- **First check**: a new KPI's first check only records a baseline; no change alert is sent. Alerts (and Mattermost messages) start with the second check that finds a change; built-in sources still post a one-line "baseline loaded" note. The Mattermost message lists the changed KPI pages with a short `+`/`-` excerpt in a code block (page text is shown as code, so it cannot format the message or @mention anyone).
- **Politeness and safety**: `robots.txt` is respected, requests are limited to 1 per second per host, and links to internal addresses (loopback, private ranges) are blocked unless the host is listed in `WATCH_ALLOWED_HOSTS`. Redirects are re-checked on every hop. In browser mode, Chromium sends all its traffic (page, images, scripts, redirects, HTTPS and WebSockets) through a small built-in proxy that applies the same block, and WebRTC UDP is disabled.

Additional environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `WATCH_ALLOWED_HOSTS` | empty | Comma-separated hosts exempt from the internal-address block (for example `127.0.0.1,localhost` for local testing) |
| `KPI_SECRET_KEY` | empty | Encrypts stored keys; required to save or use keys (see Keys) |
| `KPI_CHROMIUM_PATH` | empty | Chromium executable used for "needs a browser" KPIs; empty uses Playwright's own browser |

## Keys

A key lets a KPI link read pages or APIs that need a login or an API token. Keys live in Settings → Keys; a KPI (or a built-in source that supports it) then picks one from its "Key" list. Three types: a header (for example `Authorization: Bearer ...` or `X-Api-Key`), a link parameter (`?api_key=...`), and username and password (HTTP Basic).

**Set `KPI_SECRET_KEY` first.** Secrets are stored encrypted with it. Without it, Settings → Keys shows a setup note, saving a key returns 503, and any fetch that needs a key fails with `key unavailable (KPI_SECRET_KEY not set)`. Generate one and put it in `.env`:

```sh
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Any other string also works (it is hashed into a key), but a generated Fernet key is best. Keep it safe and backed up separately from the database.

- **Changing or losing `KPI_SECRET_KEY` makes every stored key unreadable.** They show "Needs re-entry" in Settings; re-enter each secret with "Replace secret". Nothing else is lost.
- **https only.** A key is only ever sent over `https://`; saving a KPI that has a key and an `http://` link is refused.
- **Same host only.** If a link redirects to a different host, the key is dropped for that hop (it is kept on same-host redirects).
- **Not with "Needs a browser" yet.** That combination is refused.
- **Rejected keys.** If the site answers 401 or 403 to a request that carried a key, the KPI shows "Key rejected" and counts as "not checked" in the run's `incomplete:` summary. Replace the secret to fix it. If every KPI of a source is rejected, the run fails.
- **Deleting.** A key that is still used by a KPI or source cannot be deleted; the message lists where it is used.
- **Secrets stay secret.** They never appear in API responses, logs, run errors, snapshots, raw files, previews or Mattermost messages (a stored secret is replaced by `***` if a site echoes it back).
- **Import.** The CSV/Excel import accepts an optional fourth column, `Key name` (matched ignoring case and spaces). An unknown name marks the row invalid (`unknown key: ...`).
- **Built-in sources.** Per-source keys appear only for connectors that declare key hosts.

## Attribution

Every dashboard page that shows data from a built-in source carries that source's attribution line (World Bank CC BY 4.0, SIPRI citation, IMF DataMapper). Keep these when sharing screenshots or exports; see "Data and licensing" above.

## End-to-end test

`frontend/tests/e2e/smoke.mjs` drives a real browser through the whole stack: login, overview, changes and run detail, explorer chart, KPI frequency, adding and testing a custom KPI, a baseline run then a changed-page run (the `+` excerpt), CSV import with a duplicate, and delete. It needs the api, the worker and the built frontend running. Requirements: Node 20.11 or newer, Playwright with a Chromium it can launch (the test looks for the `playwright` module via normal resolution; set `PLAYWRIGHT_MODULE` to a path if it is installed globally; the browser starts with `--no-sandbox` so it also works as root in containers), and the backend dependencies (`uv`). `dev` below is a **test-only password** for a throwaway local stack; never use it for a real deployment.

```sh
export DATABASE_URL=postgresql+psycopg://kpi:kpi@localhost:5432/kpi
export DASHBOARD_PASSWORD=dev                      # test-only password
export WATCH_ALLOWED_HOSTS=127.0.0.1,localhost     # lets the test watch its local page server
export RAW_DIR=/tmp/kpi-raw && mkdir -p "$RAW_DIR"
# optional: MATTERMOST_WEBHOOK_URL=http://127.0.0.1:8766/hook (a stub server of your own)

(cd backend && uv run alembic upgrade head)
(cd backend && exec uv run uvicorn app.api.main:app --port 8000) & API_PID=$!
(cd backend && exec uv run python -m app.worker) & WORKER_PID=$!
(cd frontend && npm run build && exec ./node_modules/.bin/vite preview --port 8080) & WEB_PID=$!

(cd frontend && npm run e2e)                       # the smoke test
(cd frontend && node tests/e2e/screenshots.mjs)    # optional: regenerate docs/screenshots/

kill $API_PID $WORKER_PID $WEB_PID                 # stop the three background servers
```

Set `KPI_CHROMIUM_PATH` to a Chromium executable if Playwright's own browser is not installed; the smoke test and the worker's "needs a browser" KPIs both use it.

The test serves its own page on 127.0.0.1:8765 (`E2E_PAGE_PORT`), removes the KPIs it creates (also on failure or Ctrl-C), and waits up to 90 s per run for the worker.

### Keys end-to-end test

`frontend/tests/e2e/keys.mjs` (`npm run e2e:keys`) adds a header key in the UI, adds a KPI that uses it, tests the link, runs it, then replaces the secret with a wrong one and checks "Key rejected" plus the `incomplete:` warning. Finally it searches the database, raw files, Mattermost bodies and logs for both secrets. It needs the same stack as above, plus:

- a self-signed certificate for `localhost`; the test serves a small HTTPS stub on 127.0.0.1:8443 (`E2E_KEYS_PORT`) that answers 200 only when it sees the right `X-Api-Key`, else 401;
- the api and worker (only those processes) trusting that certificate through `SSL_CERT_FILE`, which httpx honours: point it at the system CA bundle plus the self-signed certificate;
- a generated `KPI_SECRET_KEY` for the run.

```sh
D=/tmp/kpi-keys-e2e && mkdir -p $D
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost" -keyout $D/localhost.key -out $D/localhost.crt
cat /etc/ssl/certs/ca-certificates.crt $D/localhost.crt > $D/bundle.pem

# start api and worker as above, with these extra variables:
export KPI_SECRET_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
export SSL_CERT_FILE=$D/bundle.pem

(cd frontend && E2E_KEYS_CERT_DIR=$D npm run e2e:keys)
```

Optional extra leak checks: `RAW_DIR` (scan the raw files), `E2E_MM_LOG` (a file your Mattermost stub appends request bodies to), `E2E_LOG_FILES` (comma-separated api/worker log files) and `E2E_PSQL_URL` (a plain `postgresql://` URL, needs `psql`). The test removes its KPIs and key afterwards; the empty custom source it created stays (there is no API to delete a source).
