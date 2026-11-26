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

Every POST, PUT, PATCH and DELETE under `/api/` must also send `X-Requested-With: kpi-monitor`, or the api answers 403 `missing X-Requested-With`. Browsers resend Basic auth on a form post from another site, but such a form cannot set this header, so it blocks cross-site requests. The dashboard sends it on every call. When scripting with curl:

```sh
curl -u x:$DASHBOARD_PASSWORD -H 'X-Requested-With: kpi-monitor' -X POST localhost:8000/api/sources/{slug}/run
```

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

**Licences are yours to provide.** The tool never fetches licensed or key-protected data on its own: a source that needs a key or a login runs only after you add your own (Settings → Keys) and pick it for that source; without one, its run stops with "needs a key: add one in Settings → Keys and pick it for this source". By running a source you accept that source's terms of use. For SIPRI, commercial or business use needs your own SIPRI licence; the attribution lines below must stay either way.

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

**Chart colours.** Lines are drawn in a fixed order: Mid Green (the brand colour) first, then blue, magenta, olive, violet and teal, each with its own dash pattern, so lines stay distinguishable for colour-blind readers and in print. The brand's other greens are too light or too dark to tell six lines apart, so they are used for the interface, not for lines. Amber and red are never used for lines: they always mean "altered" and "removed". A chart shows at most 6 lines; pick which ones under the chart.

### Pages

| Page | What it does |
|---|---|
| Overview | One card per source: last run, rows tracked, changes in 30 days, "Run now" |
| Changes | Run feed filtered by source, area, change type (added/altered/removed), status and date; open a run for its change list (page changes show a `+`/`−` text excerpt) |
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
- **Delete a source**: "Delete source" on a custom source removes the source, its KPIs, their history and change records, and its saved page copies. Keys and logins are kept. It is refused while a check of that source is running; try again when it finishes. Nothing is posted to Mattermost.
- **KPI names** are unique within a source (ignoring upper/lower case); adding, renaming or importing a clashing name is refused.
- **Renaming a KPI** keeps its current value but starts its change history fresh: past changes stay in the change feed and run details under the old name, while the explorer and KPI frequency count only changes recorded after the rename.
- **First check**: a new KPI's first check only records a baseline; no change alert is sent. Alerts (and Mattermost messages) start with the second check that finds a change; built-in sources still post a one-line "baseline loaded" note. The Mattermost message lists the changed KPI pages with a short `+`/`-` excerpt in a code block (page text is shown as code, so it cannot format the message or @mention anyone).
- **Politeness and safety**: `robots.txt` is respected, requests are limited to 1 per second per host, and links to internal addresses (loopback, private ranges) are blocked unless the host is listed in `WATCH_ALLOWED_HOSTS`. Redirects are re-checked on every hop. In browser mode, Chromium sends all its traffic (page, images, scripts, redirects, HTTPS and WebSockets) through a small built-in proxy that applies the same block, and WebRTC UDP is disabled.

Additional environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `WATCH_ALLOWED_HOSTS` | empty | Comma-separated hosts exempt from the internal-address block (for example `127.0.0.1,localhost` for local testing) |
| `KPI_SECRET_KEY` | empty | Encrypts stored keys; required to save or use keys (see Keys) |
| `KPI_CHROMIUM_PATH` | empty | Chromium executable used for "needs a browser" KPIs; empty uses Playwright's own browser |
| `KPI_TEST_TRUSTED_SPKI` | empty | **Tests only, never set it in production.** Base64 SHA-256 of one certificate's public key that the browser accepts without a CA. Honoured only when `KPI_TESTING=1` is also set, and logs a warning when applied. The login-engine tests (`tests/test_logins.py`) set both for their throwaway self-signed site. It trusts only that key, never "any certificate" |

## Keys

A key lets a KPI link read pages or APIs that need a login or an API token. Keys live in Settings → Keys; a KPI (or a built-in source that supports it) then picks one from its "Key" list. Built-in sources can't use a browser login: it is only offered to KPIs. Four types: a header (for example `Authorization: Bearer ...` or `X-Api-Key`), a link parameter (`?api_key=...`), username and password (HTTP Basic), and a browser login (a site's own sign-in page; see Logins below).

**Set `KPI_SECRET_KEY` first.** Secrets are stored encrypted with it. Without it, Settings → Keys shows a setup note, saving a key returns 503, and any fetch that needs a key fails with `key unavailable (KPI_SECRET_KEY not set)`. Generate one and put it in `.env`:

```sh
cd backend && uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Any other string also works (it is hashed into a key), but a generated Fernet key is best. Keep it safe and backed up separately from the database.

- **Changing or losing `KPI_SECRET_KEY` makes every stored key unreadable.** They show "Needs re-entry" in Settings; re-enter each secret with "Replace secret". Nothing else is lost.
- **https only.** A key is only ever sent over `https://`; saving a KPI that has a key and an `http://` link is refused.
- **Same host only.** If a link redirects to a different host, the key is dropped for that hop (it is kept on same-host redirects).
- **Not with "Needs a browser".** Header, link-parameter and HTTP Basic keys cannot be combined with it. Browser logins are the exception: they always use the browser.
- **Rejected keys.** If the site answers 401 or 403 to a request that carried a key, the KPI shows "Key rejected" and counts as "not checked" in the run's `incomplete:` summary. Replace the secret to fix it. If every KPI of a source is rejected, the run fails.
- **Deleting.** A key that is still used by a KPI or source cannot be deleted; the message lists where it is used.
- **Secrets stay secret.** They never appear in API responses, logs, run errors, snapshots, raw files, previews or Mattermost messages (a stored secret is replaced by `***` if a site echoes it back).
- **Import.** The CSV/Excel import accepts an optional fourth column, `Key name` (matched ignoring case and spaces). An unknown name marks the row invalid (`unknown key: ...`).
- **Built-in sources.** Per-source keys appear only for connectors that declare key hosts.
- **Changing a key is quiet.** Picking a different key, or editing what a key sends (secret, type, header, parameter, username, extra hosts, login address), makes the next check of every source and KPI using it a silent baseline: the new data is stored without change alerts, because a different key or licence tier can return different data. If that first check finds far fewer rows than before, nothing is deleted and a warning is posted; if the next check is short too, the missing rows are held for your review as usual.
- **Not during a check.** A key change is refused while a check of an affected source is running ("try again when it finishes"). A batch of changes held for review that was fetched with the old key is set aside, so it can't be approved by mistake.

## Logins

Some sites only show their numbers after you sign in on their own sign-in page, with a username and password, and sometimes a one-time code they email you. A **browser login** lets the monitor do that sign-in for you.

**Adding a login.** In Settings → Keys, click "Add key" and choose "Browser login (site sign-in page)". Fill in:

- **Login page**: the address of the site's sign-in page. It must start with `https://`.
- **Username** and **password**: the ones you use on that site. The password is stored encrypted (set `KPI_SECRET_KEY` first, see Keys) and is never shown again.
- **Advanced** (usually not needed):
  - **Selectors**: if the monitor can't find the sign-in boxes by itself, tell it where the username box, password box, submit button and email code box are (CSS selectors, for example `#email`).
  - **Also used on**: other site names this login covers, separated by commas or spaces (for example `app.example.com, data.example.com`). Use it when you sign in on one site name but the numbers live on another.

Then add a KPI as usual and pick the login in its "Key" list. The KPI's link must be on the login page's site, or on a site listed under "Also used on"; otherwise saving is refused with a message that tells you to add it there. KPIs with a login always use the built-in browser, so "Needs a browser" is ticked for you. A login can only be used by KPIs, not by built-in sources (Settings doesn't offer it there, and the server refuses it).

Editing a login's address or its "Also used on" list is refused if an enabled KPI would no longer be covered; the message lists up to five of those KPIs. Move or disable them first.

**How it works.**

1. The first time, and whenever the site has signed you out, the monitor opens the sign-in page, types the username and password, and submits.
2. If that works, it remembers the signed-in session (stored encrypted) and checks your KPIs. Later checks reuse the session, so most checks don't sign in at all.
3. When the session runs out, the monitor signs in again by itself, as long as the site doesn't ask for a code.

**When the site emails you a code.** The monitor never reads your email. Instead:

1. It stops and marks the KPIs "Login needed". They count as "not checked", so nothing is removed and no false "page changed" alert is sent.
2. You get **one** Mattermost message: "*source* — *login* needs you to sign in", with a link to the dashboard. You won't get one per KPI or per run. KPIs waiting for a sign-in don't make the check fail: even when every KPI of a source is waiting, there is no "check failed" or "failed N times in a row" message, and no separate "incomplete fetch" line for them.
3. Open the link (or Settings → Keys) and click **Sign in now** on the login. The Overview page also shows a "Sign-in needed" flag on the affected source.
4. The monitor signs in again and the site sends you a new code. The dialog says "We sent a code to your email. Enter it within 10:00" and counts down from **10 minutes**.
5. Type the code and click Submit. On success the dialog says "Signed in. Checking your KPIs now", and the KPIs that were waiting are checked straight away.
6. If the code is wrong or too late, or something else goes wrong, the dialog says what happened. Click "Try again" (or "Sign in now") to start over. Only one sign-in per login can run at a time.

Until you sign in, the monitor does not keep trying by itself, so the site doesn't keep emailing you codes or lock your account.

**Status labels.** Each login in Settings → Keys shows one of: "Signed in", "Login needed" or "Signing in…". When a sign-in didn't work, the login shows "Login needed" with the reason (for example "code was not accepted").

**What isn't supported.**

- Reading codes from your email or text messages automatically.
- CAPTCHAs ("I'm not a robot" checks).
- "Sign in with Google/Microsoft/…" and other single sign-on through another company's sign-in page.
- Sign-in pages that aren't `https://`.

**Safety.** The password, codes and the saved session's cookies are never shown, logged, or saved in plain text. They are kept out of run errors, raw files, page snapshots and Mattermost. A code is kept (encrypted) only until it has been typed in, then wiped. The password is only ever typed into the saved login page's site (or an "Also used on" site), and the saved session is only used for those sites. The login site's own page scripts are trusted, as in any browser you sign in with.

**robots.txt.** The KPI pages behind a login are checked against the site's robots.txt as usual, but the sign-in page itself is not: sites often disallow `/login` for crawlers, and signing in is something you asked the monitor to do on your behalf.

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

export WATCH_ALLOWED_HOSTS=127.0.0.1,localhost   # the stub is on localhost
export KPI_SECRET_KEY=$(cd backend && uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
# plus DATABASE_URL, DASHBOARD_PASSWORD, RAW_DIR as above, and MATTERMOST_WEBHOOK_URL pointing at your stub

# SSL_CERT_FILE only for these two processes, never exported to the shell:
(cd backend && SSL_CERT_FILE=$D/bundle.pem exec uv run uvicorn app.api.main:app --port 8000 > $D/api.log 2>&1) & API_PID=$!
(cd backend && SSL_CERT_FILE=$D/bundle.pem exec uv run python -m app.worker > $D/worker.log 2>&1) & WORKER_PID=$!

(cd frontend && E2E_KEYS_CERT_DIR=$D E2E_MM_LOG=/tmp/mm.log E2E_LOG_FILES=$D/api.log,$D/worker.log \
  E2E_PSQL_URL=postgresql://kpi:kpi@localhost:5432/kpi npm run e2e:keys)
```

The leak scan always covers every place, so these are all required (the test exits with code 2 and names what is missing): `RAW_DIR` (the raw files), `E2E_MM_LOG` (a file your Mattermost stub appends request bodies to), `E2E_LOG_FILES` (comma-separated api/worker log files), `E2E_PSQL_URL` (a plain `postgresql://` URL; needs `psql`) and `E2E_KEYS_CERT_DIR`. The test removes its KPIs and key afterwards; the empty custom source it created stays (there is no API to delete a source).

### Logins end-to-end test

`frontend/tests/e2e/logins.mjs` (`npm run e2e:logins`) checks browser logins on the real stack. It serves a small HTTPS sign-in site with an email-code step. Instead of emailing, the site writes each code to a file and the test reads it, standing in for your inbox. The test:

1. adds a login (through the API);
2. adds a KPI on the protected page;
3. runs it: the site asks for a code, so the KPI becomes "Login needed" and exactly one Mattermost post is sent for the source, the sign-in message (a second run sends nothing: no "check failed");
4. clicks "Sign in now" in the dashboard, types a wrong code (the dialog says it was not accepted), clicks "Try again", types the new code from the file, and waits for "Signed in"; the KPI is then checked straight away and baselined silently in that run (status `no_change`, or `baseline` on a brand-new source);
5. changes the protected page and runs it: `ok`, with a `+` excerpt, reusing the saved session;
6. makes the site sign everyone out and stop asking for codes: the next run signs in again by itself;
7. searches the database, raw files, Mattermost bodies, logs and API responses for the password, every code and every session cookie;
8. removes its KPI and login.

It needs the Keys test's setup (certificate, `KPI_SECRET_KEY`, Mattermost stub), plus a browser for the worker and a test-only trust of the certificate:

```sh
D=/tmp/kpi-logins-e2e && mkdir -p $D   # localhost.crt + localhost.key made as in the Keys test
export KPI_CHROMIUM_PATH=/path/to/chrome   # if Playwright's own browser is not installed
# TEST ONLY, never in production: the worker's browser trusts exactly this certificate's key
SPKI=$(openssl x509 -in $D/localhost.crt -pubkey -noout | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64)
(cd backend && KPI_TESTING=1 KPI_TEST_TRUSTED_SPKI=$SPKI exec uv run python -m app.worker > $D/worker.log 2>&1) & WORKER_PID=$!
# the api as in the Keys test, logging to $D/api.log

(cd frontend && E2E_LOGINS_CERT_DIR=$D E2E_MM_LOG=/tmp/mm.log E2E_LOG_FILES=$D/api.log,$D/worker.log \
  E2E_PSQL_URL=postgresql://kpi:kpi@localhost:5432/kpi_e2e npm run e2e:logins)
```

The site listens on 127.0.0.1:8444 (`E2E_LOGINS_PORT`). The same variables as the Keys test are required (`RAW_DIR`, `E2E_MM_LOG`, `E2E_LOG_FILES`, `E2E_PSQL_URL`, here with `E2E_LOGINS_CERT_DIR`). If `KPI_TEST_TRUSTED_SPKI` is set in the test's own shell, it must match the certificate. Use a throwaway database such as `kpi_e2e`, never your real one. The empty custom source stays afterwards, as in the Keys test.
