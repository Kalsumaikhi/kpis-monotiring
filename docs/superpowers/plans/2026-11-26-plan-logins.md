# KPI Monitor — Browser Logins with Manual Email Codes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Watch pages that sit behind a username/password login, sometimes followed by an emailed one-time code. The tool signs in by itself when no code is needed, and asks the user on the dashboard when one is (spec §5.11).

**Architecture:**
- A new key kind `login` lives in the existing `credentials` store.
- `app/logins.py` holds the login engine, built on the existing Playwright browser path and its destination-policy proxy. It covers form detection, sign-in, code detection, session save/load and expiry detection.
- New tables: `login_sessions` (encrypted Playwright `storage_state`) and `login_attempts` (interactive sign-ins with a 10-minute code window).
- `WatchConnector` uses the session for targets that have a login key, and re-logs in by itself when no code is needed.
- The worker runs interactive attempts.
- The dashboard gets a "Login needed" status, a "Sign in now" button and a code dialog.

**Tech Stack:** the existing FastAPI/SQLAlchemy/Alembic backend, Playwright for Python (browser extra, `KPI_CHROMIUM_PATH`), Fernet via `app/credentials.py`, and the Vue 3 frontend.

**Spec:** `docs/superpowers/specs/2026-11-24-kpi-monitor-design.md` §5.11, plus §5.10 for keys and §5.9 for custom sources.

**Base:** branch `feat/logins` from `main` (9b278a4).

## Global Constraints

- **Login key fields:**
  - Kind `login`: `login_url` (https only, required), `username` (required), the secret is the password (at least 8 characters, as for all keys).
  - Optional `selectors` JSON: `{username, password, submit, code}` CSS selectors.
  - Optional `extra_hosts` (list of exact hostnames).
  - Validation messages:
    - `login page must be https`
    - `username is required`
    - `invalid selector: {name}`
    - `invalid host: {h}`
- **Allowed hosts:** a target with a login key must have a link host equal to the login URL's host or listed in `extra_hosts`. Otherwise reject with 422 `{"detail": "this login is for {host}; add the KPI's site under \"Also used on\""}`.
- **Browser mode:** a target with a login key is always fetched in browser mode. The API sets `use_browser=true` itself. Non-login keys keep the existing rule that they cannot be used with browser mode.
- **Session storage:** the session is Playwright `storage_state` JSON, Fernet-encrypted in `login_sessions.state_enc`.
  - Before loading, cookies and origins are filtered to the login's allowed hosts and their parent-domain cookies only.
  - The session is only loaded for fetches whose URL host is allowed for that login.
- **Expired session:** after navigation, the page counts as expired when either:
  - the final URL's host+path equals the `login_url` host+path; or
  - a visible `input[type=password]` exists and the page was not expected to be the login page.
- **Form detection** (when no selector is given):
  - password: the first visible `input[type=password]`;
  - username: the nearest preceding visible input of type `email`, `text` or `tel` in the same form;
  - submit: the first `button[type=submit]`/`input[type=submit]` in that form, otherwise press Enter in the password box.
- **Code detection** (after submit, wait up to 15 s for navigation or idle): a visible input that is not a password box and matches any of:
  - `autocomplete="one-time-code"`;
  - name, id or placeholder matching `/otp|code|token|verif|pin/i`;
  - `inputmode="numeric"` with `maxlength` ≤ 8.

  The `selectors.code` selector overrides detection.
- **Statuses:**
  - Target `last_status` can also be `login_needed` (counts as not checked in the `incomplete:` summary, with error `login needed`).
  - Login status (`login_sessions.status`): `ok` | `login_needed` | `signing_in` | `waiting_code` | `failed`.
  - Attempt statuses: `pending` | `signing_in` | `waiting_code` | `verifying` | `succeeded` | `failed` | `expired` | `cancelled`.
- **Code window:** 10 minutes from entering `waiting_code`. The code is stored Fernet-encrypted in `login_attempts.code_enc` until it is typed in, then set to NULL. Only one active attempt per login: a second start returns 409 `{"detail": "a sign-in is already in progress"}`.
- **Mattermost:** one message per transition of a login into `login_needed`: `**{source}** — {login name} needs you to sign in. Open the dashboard to enter the email code.\n[Sign in]({DASHBOARD_BASE_URL}/settings#logins)`. `{source}` is the first affected source name, and `{login name}` is neutralised for mentions as in other messages.
- **Secrets:** passwords, codes, cookie values and storage values must never appear in API responses, logs, `run.error`, raw files, snapshots or Mattermost. Reuse `creds.redact`/`redact_bytes` with the password, plus the code while it is known.

## Review Focus

1. **A login page is mistaken for content.** Signing in fails silently and the login page itself is stored as the KPI content. Expectation: if the page still shows a login form after a sign-in attempt, the target is `login_needed`, never `ok`, so no false "page changed" alert. Test: Task 3 `test_login_page_never_becomes_content`.
2. **A cookie leaks to another site.** The session holds cookies for `ads.tracker.com` because the login page loaded it. Those cookies must not be loaded into a fetch for another login's KPI or a non-login KPI. Test: Task 2 `test_session_filtered_to_allowed_hosts`.
3. **A code arrives after the 10-minute window.** Expect 410 `{"detail": "this sign-in expired — click Sign in now again"}`, with the stored code never kept. Test: Task 4 `test_code_after_expiry_410`.
4. **The worker restarts while an attempt is waiting for a code.** On startup, stale `signing_in`/`waiting_code`/`verifying` attempts become `failed` with `worker restarted — click Sign in now again`, and the code is wiped. Test: Task 4 `test_sweep_stale_attempts`.
5. **Many KPIs share one login and the session expires.** Expect one Mattermost message, not one per KPI or per run. Test: Task 3 `test_login_needed_alert_once`.

---

### Task 1: Login key kind and tables

**Files:**
- Create: `backend/migrations/versions/0008_logins.py`, `backend/tests/test_login_keys.py`
- Modify: `backend/app/models.py`, `backend/app/api/routes_credentials.py`, `backend/app/credentials.py` (the `Credential` dataclass gains `login_url`, `selectors`, `extra_hosts`)

**Interfaces:**
- Produces:
  - `credentials` gains `login_url text null`, `selectors jsonb null` and `extra_hosts jsonb null`.
  - `KIND_FIELDS["login"] = {login_url, username, selectors, extra_hosts}`.
  - `login_sessions(credential_id PK FK→credentials ON DELETE CASCADE, state_enc bytea null, status text default 'login_needed', last_error text null, updated_at, notified_at null)`.
  - `login_attempts(id, credential_id FK ON DELETE CASCADE, status text, code_enc bytea null, error text null, created_at, waiting_since null, finished_at null)`, plus a partial unique index allowing one active attempt per credential (status in pending/signing_in/waiting_code/verifying).
  - The API accepts and returns the new fields for kind `login` (never the password), with validation per Global Constraints.
  - `GET /api/credentials` items for logins add `login_status` and `login_error`.
  - `creds.allowed_hosts(cred) -> set[str]` (the login host plus `extra_hosts`, lowercased).

- [ ] **Step 1: Write the failing tests:**
  - `test_create_login_key`
  - `test_login_url_must_be_https`
  - `test_login_requires_username`
  - `test_extra_hosts_validation`
  - `test_selectors_validation`, which rejects selectors longer than 200 characters or containing newlines
  - `test_login_status_in_list`
  - `test_password_never_returned`
- [ ] **Step 2: Run them.** Command: `cd backend && TEST_DATABASE_URL=postgresql+psycopg://kpi:kpi@localhost:5432/kpi_test uv run pytest tests/test_login_keys.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Write migration 0008 (down_revision 0007) with a full downgrade.
- [ ] **Step 4: Run the full backend suite.** Expected: PASS. Apply migration 0008 to the dev DB (upgrade only).
- [ ] **Step 5: Commit.** `feat: login key kind, sessions and attempts tables`

---

### Task 2: Login engine

**Files:**
- Create: `backend/app/logins.py`, `backend/tests/test_logins.py`, `backend/tests/login_site.py` (a test helper: a small https-capable login site. It serves a form, an optional email-code step (writing the code to a file the test reads), a protected page, and an expiry switch.)
- Modify: `backend/app/connectors/watch/fetch.py` (factor out what the browser path needs: proxy setup and launch options as reusable helpers)

**Interfaces:**
- Consumes: the Task 1 models and `Credential` fields; the existing `PolicyProxy` and `destination_allowed`.
- Produces:
  - `class LoginNeedsCode(Exception)` and `class LoginFailed(Exception)` (message already redacted).
  - `load_state(session, cred) -> dict | None` and `save_state(session, cred, state: dict) -> None`. Both filter to allowed hosts and encrypt.
  - `filter_state(state: dict, hosts: set[str]) -> dict`: keep cookies whose domain is a host in `hosts` or a parent domain of one, and origins whose host is in `hosts`.
  - `browser_fetch(url, cred, state: dict | None) -> tuple[FetchResult, dict | None, bool]`: returns the result, the new state and `expired`. It uses the proxy, applies the session only when the URL host is allowed, detects expiry per Global Constraints and never returns login-page content as ok.
  - `sign_in(cred, code_provider: Callable[[], str | None] | None, timeout_s=600) -> dict`:
    - Returns the new `storage_state`.
    - Raises `LoginNeedsCode` when a code box appears and `code_provider` is None.
    - With a `code_provider`, polls it every 2 s until it returns a code or the timeout passes, then types the code and submits.
    - Raises `LoginFailed("login failed: still on the login page")`, `LoginFailed("code was not accepted")` or `LoginFailed("timed out waiting for the code")` as appropriate.
  - All browser work goes through the destination policy. Navigation to `login_url` requires https and an allowed destination.

- [ ] **Step 1: Write the failing tests.** These are real Playwright tests against `login_site.py`, skipped if Chromium is unavailable, with `WATCH_ALLOWED_HOSTS` covering 127.0.0.1/localhost and an https cert as in the keys e2e or the site served over https via a test cert:
  - `test_sign_in_no_code`
  - `test_sign_in_needs_code_raises_without_provider`
  - `test_sign_in_with_code_provider`
  - `test_wrong_code_fails`
  - `test_code_timeout`
  - `test_browser_fetch_with_valid_session`
  - `test_browser_fetch_detects_expiry`
  - `test_session_filtered_to_allowed_hosts`
  - `test_form_detection_without_selectors`
  - `test_custom_selectors`
  - `test_password_not_in_any_output`
- [ ] **Step 2: Run them.** Command: `KPI_CHROMIUM_PATH=/opt/pw-browsers/chromium-1243/chrome-linux64/chrome uv run --extra browser pytest tests/test_logins.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full backend suite,** with and without `KPI_CHROMIUM_PATH`, so the skips work.
- [ ] **Step 5: Commit.** `feat: browser login engine (form detection, sessions, email-code step)`

---

### Task 3: Login-backed watch fetches and the "login needed" alert

**Files:**
- Modify: `backend/app/connectors/watch/connector.py`, `backend/app/connectors/watch/fetch.py`, `backend/app/api/routes_watch.py`, `backend/app/notify/mattermost.py`, `backend/app/core/pipeline.py` (only if the hook needs it)
- Test: `backend/tests/test_login_watch.py`

**Interfaces:**
- Produces:
  - Target create, patch and import with a login key: force `use_browser=true` and enforce the allowed-hosts 422. Non-login keys with browser mode are still refused.
  - `WatchConnector.download`, for targets with a login key, grouped by login:
    - Load the state once per login and call `logins.browser_fetch` per target.
    - On expiry, try `sign_in(cred, None)` once per login per run:
      - Success: save the state and refetch.
      - `LoginNeedsCode` or `LoginFailed`: set the login status to `login_needed` with the error, and mark that login's targets `login_needed` (error `login needed`).
    - Save refreshed state after successful fetches.
    - Never store login-page HTML as content.
  - Notification: when a login moves into `login_needed` (`notified_at` is null or older than its last `ok`), the run's notifier extras gain `login_needed: [{login_name, source}]`, and Mattermost appends the exact message from Global Constraints once per login. `notified_at` is set when that happens.
  - Raw JSON and snapshots go through `redact` with the password.

- [ ] **Step 1: Write the failing tests:**
  - `test_login_target_forced_browser`
  - `test_login_host_must_match_422`
  - `test_scheduled_run_auto_relogin_without_code`
  - `test_code_required_marks_login_needed_incomplete` (data kept, nothing removed)
  - `test_login_needed_alert_once`, covering 3 KPIs, 2 runs, and exactly one message
  - `test_login_page_never_becomes_content`
  - `test_alert_again_after_recovery_then_expiry`
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full backend suite.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat: watch KPIs behind browser logins; one alert when a sign-in is needed`

---

### Task 4: Interactive "Sign in now" with the email code

**Files:**
- Create: `backend/app/api/routes_logins.py`, `backend/tests/test_login_attempts.py`
- Modify: `backend/app/worker.py`, `backend/app/api/main.py`

**Interfaces:**
- Produces:
  - `POST /api/logins/{credential_id}/sign-in` → 202 `{attempt_id, status: "pending"}`.
    - 409 when an attempt is already active.
    - 422 when the key is not a login.
    - 503 when `KPI_SECRET_KEY` is not set.
  - `GET /api/logins/{credential_id}/attempt` → `{attempt_id, status, error, waiting_since, expires_at}` for the latest attempt. It never includes the code.
  - `POST /api/logins/attempts/{attempt_id}/code` with `{code}` (1–64 characters, `[A-Za-z0-9-]`):
    - 202 when the attempt is `waiting_code`;
    - 410 with the Global Constraints message after expiry;
    - 409 in any other state.
    - The code is stored encrypted.
  - `POST /api/logins/attempts/{attempt_id}/cancel` → `cancelled`, with the code wiped.
  - Worker:
    - The poller picks up `pending` attempts, one thread per attempt, at most 2 concurrently.
    - It runs `sign_in(cred, code_provider)`. The `code_provider` reads, decrypts and wipes `code_enc`, setting the status to `verifying`.
    - Statuses move as the attempt progresses.
    - On success: save the state, set the login status to `ok`, clear `notified_at`, and queue a run for each source with targets using this login.
    - On failure: `failed`, with a redacted error.
    - Expiry is enforced at 10 minutes.
  - On worker startup, sweep stale attempts per Review Focus 4.
  - The session status in `GET /api/credentials` reflects all of this.

- [ ] **Step 1: Write the failing tests.** Drive the worker functions directly, with `sign_in` faked except in one real-browser test:
  - `test_start_attempt_202_and_409_when_active`
  - `test_attempt_flow_waiting_code_then_success_queues_runs`
  - `test_code_after_expiry_410`
  - `test_wrong_code_failed`
  - `test_cancel_wipes_code`
  - `test_sweep_stale_attempts`
  - `test_code_never_in_responses_or_logs`
  - `test_real_browser_code_flow` (skips without Chromium), against `login_site.py`
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full backend suite.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat: interactive sign-in with email code (10-minute window)`

---

### Task 5: Frontend

**Files:**
- Create: `frontend/src/pages/settings/LoginsPanel.vue` (or a section inside KeysCard), `frontend/src/components/SignInDialog.vue`, tests
- Modify: `frontend/src/api/types.ts`, `KeyForm.vue` (Browser login type: login page URL, username, password, Advanced selectors and "Also used on"), `TargetForm.vue` (a login key forces "Needs a browser" on, disabled with the hint "Logins always use the browser"; the allowed-host message shows inline), `KeysCard.vue` (a login status badge and "Sign in now"), `StatusBadge.vue` (`login_needed`: "Login needed", warn, with a lock icon), `OverviewPage.vue` (a "Sign-in needed" flag linking to `/settings#logins`)

**Interfaces:**
- Produces:
  - **SignInDialog** shows these states:
    - starting / signing in: a spinner and "Signing in to {host}…";
    - `waiting_code`: a code input (`autocomplete="one-time-code"`, `inputmode="numeric"`), "We sent a code to your email. Enter it within {mm:ss}.", a live countdown, and Submit / Cancel;
    - verifying;
    - succeeded: "Signed in. Checking your KPIs now.";
    - failed / expired: the message and "Try again".
  - The dialog polls the attempt every 2 s while open and stops on close or a final state.
  - The code input is cleared after submit. Codes are never stored on the client.
  - `/settings#logins` scrolls to the Keys card.

- [ ] **Step 1: Write the failing tests:**
  - `login key form fields and validation`
  - `target form forces browser for login key`
  - `sign-in dialog full flow with countdown`
  - `expired shows try again`
  - `code input cleared after submit`
  - `overview shows sign-in needed`
  - `StatusBadge login_needed`
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Follow the frontend conventions; screenshots at 1280 and 390 of the login key form, a waiting-code dialog, and Settings with "Login needed".
- [ ] **Step 4: Verify.** Run `npx vitest run && npx vue-tsc --noEmit && npm run build`.
- [ ] **Step 5: Commit.** `feat(frontend): browser logins and sign-in with email code`

---

### Task 6: End-to-end and docs

**Files:**
- Create: `frontend/tests/e2e/logins.mjs` (npm script `e2e:logins`)
- Modify: `README.md` (a Logins section)

- [ ] **Step 1: Real stack.** Run api and worker as in the keys e2e, with `KPI_CHROMIUM_PATH` set, and an https login site stub that has an email-code step (the code is written to a file the test reads, standing in for the user's inbox). Then:
  1. Add a login key.
  2. Add a KPI on the protected page.
  3. Run now → `login_needed` plus one Mattermost message.
  4. Sign in now → the dialog asks for the code; the test reads it from the file and enters it → succeeded → the KPI is checked automatically (baseline).
  5. Change the protected page and run → ok, with an excerpt.
  6. Expire the session on the stub with no code required → the next run signs in again by itself.
  7. Leak scan for the password and code across the DB, raw files, logs and Mattermost.
  8. Clean up.
- [ ] **Step 2: README.** How logins work, the code flow, what isn't supported, and the "Also used on" field.
- [ ] **Step 3: Run every suite:** backend, vitest, vue-tsc, and all three e2e scripts.
- [ ] **Step 4: Commit.** `test: browser logins end-to-end; docs`
