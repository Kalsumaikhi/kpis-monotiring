# Plan 3a: Loose Ends and Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the decisions the user settled on 2026-11-26 and the review findings deferred from Plans 2, keys and logins.

**Architecture:** These are small, independent changes to the existing FastAPI backend and Vue frontend. There is no new subsystem, and migrations are added only where a constraint is missing.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2, Alembic, Postgres 16, Vue 3.5 + TS, Vitest, Playwright e2e scripts.

**Spec:** docs/superpowers/specs/2026-11-24-kpi-monitor-design.md (§5.4, §5.8 dashboard, §5.9 custom sources, §5.10 keys, §5.11 logins).

## User decisions (2026-11-26)

- **Licences:** "All licenses will be provided through the user." The tool never fetches licensed data without a user-supplied key or login. It states the user's responsibility for each source's terms, SIPRI's commercial licence included.
- **Chart colours:** "do as u could." Keep the validated palette in `frontend/src/styles/tokens.css` (`--series-1..6`). The only change is a short README note explaining it.
- **Everything else:** "Fix everything as u see fit."

## Global Constraints

- Secrets (key values, passwords, codes, session cookies) never appear in logs, errors, API responses, Mattermost or raw files (spec §5.10/§5.11).
- One Mattermost message per run or state change, never one per data point.
- No false "altered" or "removed" alerts. Deleting or disabling things is silent.
- Palette tokens only. No new hex colours outside tokens.css.
- Never touch or downgrade the dev DB `kpi`. Tests use `kpi_test`; the e2e uses `kpi_e2e`.

## Review Focus

1. **Deleting a custom source while its run is in progress** must wait for or refuse the run. It must never leave orphan rows, and must never 500.
2. **Cross-site POST** (a form on another site posting to `/api/...` with the browser's cached Basic auth) must be refused for every unsafe method.
3. **Open redirect** after login (`next=/.//evil.com`, `next=//evil.com`, `next=https://evil.com`) must always land inside the app.
4. **Changing a source's key while its run is in progress** must still give the next run a silent baseline, with no "N altered" flood.
5. **Change-feed filters** with unknown values (`area=xyz`, `change=foo`) must return an empty list or a 422. They must not return a 500 or silently ignore the filter.

---

### Task 1: Delete a custom source

**Files:**
- Modify: `backend/app/api/routes_watch.py` (add `DELETE /api/custom-sources/{slug}`)
- Modify: `frontend/src/pages/settings/` (the custom source panel: a "Delete source" button with ConfirmDialog)
- Test: `backend/tests/test_watch_api.py`, `frontend/src/__tests__/` (the existing custom-sources spec)

**Interfaces:**
- Produces: `DELETE /api/custom-sources/{slug}`, which returns 204. It returns 404 for an unknown slug or a built-in source. It returns 409 `"a check is running for this source; try again when it finishes"` when the per-source advisory lock (namespace 7421) cannot be taken with try-lock.

- [ ] **Step 1: Write the failing tests:**
  - `test_delete_custom_source_removes_everything`: it removes the source, its targets, snapshots, runs, changes, data_points, pending_changes, change_rollup and raw files on disk, and leaves other sources' rows untouched.
  - `test_delete_custom_source_refuses_builtin` (404).
  - `test_delete_custom_source_409_while_locked`: hold the advisory lock in another connection.
  - `test_delete_custom_source_silent`: no Mattermost post.
- [ ] **Step 2:** Run them and see them fail.
- [ ] **Step 3: Implement.**
  - Take the source try-lock first.
  - Delete the child tables explicitly in FK order: tables without `ON DELETE CASCADE` (runs, changes, data_points and the rest; see models.py) are deleted by hand.
  - Remove the raw files after the commit.
  - Logins and keys used by the source stay, because they are shared.
- [ ] **Step 4: UI.** Add a "Delete source" danger button. The ConfirmDialog copy reads: "Delete {name}? Its KPIs, history and change records are removed. Keys and logins are kept." Show the 409 message inline. Write the vitest first.
- [ ] **Step 5:** Run the backend tests and vitest. They should pass.
- [ ] **Step 6:** Commit `feat: delete a custom source`.

### Task 2: Change-feed filters for area and change type (spec §5.8 item 2)

**Files:**
- Modify: `backend/app/api/routes_runs.py:53-76` (`list_runs`)
- Modify: `frontend/src/pages/ChangeFeedPage.vue`
- Test: `backend/tests/test_api.py`, `frontend/src/__tests__/` (the change-feed spec)

**Interfaces:**
- Produces two new query params on `GET /api/runs`:
  - `area: Literal['economic','military','social','cultural','political','mixed'] | None` (use the distinct `sources.area` values actually present; validate against them with a 422);
  - `change: Literal['added','altered','removed'] | None`, which matches runs where that counter is > 0.

- [ ] **Step 1: Write the failing tests:**
  - `test_runs_filter_area`;
  - `test_runs_filter_change_type`;
  - `test_runs_filter_unknown_area_422`;
  - `test_runs_filter_unknown_change_422`.
- [ ] **Step 2:** Implement. Join `Source` (already joined) and filter on `Source.area`.
- [ ] **Step 3: Frontend.**
  - Add two selects, Area and Change type, next to the Source and Status selects.
  - Keep them in the URL query as `area` and `change`, reset the page, and use `router.push` so each filter change is a history entry (deferred minor from Plan 2 Task 10).
  - Write the vitest first: changing a select calls `/api/runs` with the param.
- [ ] **Step 4:** Run the suites. They should pass.
- [ ] **Step 5:** Commit `feat(feed): filter by area and change type`.

### Task 3: Request hardening (CSRF and the open redirect)

**Files:**
- Modify: `backend/app/api/main.py` (middleware)
- Modify: `frontend/src/api/client.ts` (send the header on every request)
- Modify: the login page's `safeNext` (in `frontend/src/pages/LoginPage.vue` or `frontend/src/lib/`)
- Test: `backend/tests/test_api.py`; the vitest for `safeNext`

**Interfaces:**
- Produces: every POST, PUT, PATCH and DELETE under `/api/` must carry `X-Requested-With: kpi-monitor`, or the server returns 403 `"missing X-Requested-With"`.
  - Multipart uploads go through `client.ts` too, so they get the header.
  - GET and HEAD are unaffected.
  - Update every e2e script and the test client: add a default header in the conftest client fixture.

- [ ] **Step 1: Write the failing tests:**
  - `test_unsafe_method_without_header_403` covers POST /api/sources/x/run, POST /api/logins/1/sign-in and DELETE /api/watch-targets/1;
  - `test_get_without_header_ok`;
  - a vitest that `apiPost` sends the header.
- [ ] **Step 2:** Implement the middleware and the client header. Update the conftest client and the e2e scripts (`smoke.mjs`, `keys.mjs`, `logins.mjs` use raw fetch for setup).
- [ ] **Step 3: `safeNext` vitest cases.**
  - `'/.//evil.com'`, `'//evil.com'`, `'/\\evil.com'`, `'https://evil.com'` and `'javascript:alert(1)'` all give `'/'`.
  - `'/feed?x=1'` is kept.
  - Implement by resolving with `new URL(next, location.origin)` and requiring the same origin, then returning `pathname+search+hash`.
- [ ] **Step 4:** Run all suites, including the three e2e scripts. They should pass.
- [ ] **Step 5:** Commit `fix(security): require X-Requested-With on unsafe API calls; strict post-login redirect`.

### Task 4: Key-change baseline race (keys M9) and a supersede gap for held runs

**Files:**
- Modify: `backend/app/api/routes_sources.py` (`patch_source`, around 171-205) and `backend/app/api/routes_credentials.py` (the replace-secret path that sets needs_baseline)
- Modify: `backend/app/core/pipeline.py` (where needs_baseline is read and cleared)
- Test: `backend/tests/test_source_keys.py`

**The problem.** A key change sets the "needs baseline" flag without the source lock. A run that is already fetching with the old key then finishes, and clears the flag. The next run, with the new key, diffs against old-key data and alerts. A key may unlock more data, which gives a flood.

**The fix.** Don't clear a flag. Use a counter compare:
- Add `sources.baseline_epoch int not null default 0` (migration 0009). Bump it on a key change.
- The pipeline reads the epoch at run start and stores it on the run (`runs.baseline_epoch`).
- After the run, `needs_baseline` is cleared only if the source's epoch still equals the run's epoch (a compare-and-set UPDATE).
- Mirror this for watch targets if their key-change path has the same race. Check `routes_watch.py` PATCH target `credential_id`.
- **Held runs:** a `suspect_pending` run from before the key change must be superseded when the key changes, because approving it would apply old-key data. Set its status to `superseded`, the same as a newer run does.

- [ ] **Step 1: Write the failing tests:**
  - `test_key_change_during_run_keeps_baseline`: simulate it by bumping the epoch between the run's start and its finish, then assert needs_baseline is still true and the next run records no changes;
  - `test_key_change_supersedes_held_run`.
- [ ] **Step 2:** Write migration 0009 (with a downgrade), then the implementation.
- [ ] **Step 3:** Run the backend suite and see it pass.
- [ ] **Step 4:** Commit `fix(keys): key change always gives a silent baseline; supersede held runs`.

### Task 5: Deferred minors bundle

Each item gets a test first.

**Files:**
- Backend: `routes_watch.py`, a migration (reuse 0009 if Task 4 has not merged; otherwise 0010), `logins.py`, `login_attempts.py`, `routes_credentials.py`, `connectors/watch/connector.py`
- Frontend: `SignInDialog.vue`

**Items:**
1. **Unique KPI names per source.** Add a unique index on `watch_targets (source_id, lower(kpi_name))`.
   - Migration: dedupe first by suffixing " (2)", " (3)" in id order, and log the renames.
   - The API returns 409 `"a KPI with this name already exists in this source"` on a duplicate, both on create and on import confirm.
2. **PATCH or DELETE of a watch target takes the source lock** (try-lock, 409 when busy), and refreshing after the lock is taken returns a 404 rather than a 500 if the target was deleted meanwhile.
3. **Login edits with dependants.**
   - PATCH of a login key that changes the login host, or removes an "Also used on" host still used by an enabled target, returns 409.
   - The 409 lists the affected KPI names, at most 5, with "+N more".
4. **Shared login.** In `connector.py`, when the compare-and-set fails because another source re-signed in meanwhile, reload, and if the reloaded status is `ok`, refetch once with the new session instead of marking the target `login_needed`.
5. **Prune login attempts.** The startup sweep deletes finished `login_attempts` older than 30 days.
6. **Redact cookie values.** `browser_fetch` redacts the seeded state's cookie values (length ≥ 8) from the body, alongside the password.
7. **SignInDialog focus.** When the opener is `document.body`, don't restore focus to it; use the `main` fallback.
   - A backdrop click on the locally expired view closes the dialog.
   - Remove the double announcement: no `role=alert` inside the live region.

- [ ] **Step 1:** For each item, write the failing test, implement the fix, and see the test pass. Commit per item, or per pair of related items.
- [ ] **Step 2:** Run every suite: backend with Chromium, vitest, vue-tsc and the three e2e scripts.

### Task 6: Docs

**Files:**
- Modify: `README.md`

Add or change these:
- **"Data and licensing":** state the rule from the user decision. Licensed or keyed sources run only with the user's own key or login. Running the tool means the user accepts each source's terms. SIPRI commercial use needs the user's SIPRI licence, and the attribution lines stay.
- **"Charts":** the colour rule. Series 1 is Mid Green, then blue, magenta, olive, violet and teal, each with its own dash pattern. Amber and red are kept for "altered" and "removed".
- **Delete a source** and the new feed filters.
- **The `X-Requested-With` requirement** for anyone scripting the API.

Commit `docs: licences, chart colours, delete source, feed filters, API header`.
