// End-to-end test of browser logins against a running stack (api :8000, worker, `vite preview` :8080).
// Run: npm run e2e:logins   (see README, "Logins end-to-end test"). Playwright comes from NODE_PATH / global install.
// The test serves a local HTTPS login site with an email-code step. The "email" is a file: the site writes
// each code there and the test reads it, standing in for the user's inbox. It then checks that the password,
// the codes and the session cookies never leak into the database, raw files, Mattermost messages or logs.
import { createRequire } from 'node:module'
import https from 'node:https'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import crypto from 'node:crypto'
import { execFileSync } from 'node:child_process'
import assert from 'node:assert/strict'

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')

const BASE = process.env.E2E_BASE_URL || 'http://localhost:8080'
// 'dev' is a TEST-ONLY password for a throwaway local stack. Never use it (or this default) anywhere real.
const PASSWORD = process.env.DASHBOARD_PASSWORD || 'dev'
const SITE_PORT = Number(process.env.E2E_LOGINS_PORT || 8444)
const CERT_DIR = process.env.E2E_LOGINS_CERT_DIR // holds localhost.crt + localhost.key (see README)
// All of these are required so the leak scan always covers every place a secret could land.
const RAW_DIR = process.env.RAW_DIR
const MM_LOG = process.env.E2E_MM_LOG // file the Mattermost stub appends request bodies to
const LOG_FILES = (process.env.E2E_LOG_FILES || '').split(',').filter(Boolean) // api and worker log files
const PSQL_URL = process.env.E2E_PSQL_URL // plain postgresql:// URL, scanned with psql
const SHOTS = process.env.E2E_SCREENSHOTS || fs.mkdtempSync(path.join(os.tmpdir(), 'kpi-e2e-logins-shots-'))
const missing = [
  ['E2E_LOGINS_CERT_DIR', CERT_DIR], ['RAW_DIR', RAW_DIR], ['E2E_MM_LOG', MM_LOG],
  ['E2E_LOG_FILES', LOG_FILES.length], ['E2E_PSQL_URL', PSQL_URL],
].filter(([, v]) => !v).map(([k]) => k)
if (missing.length) {
  console.error(`Missing required environment: ${missing.join(', ')}. See README, "Logins end-to-end test".`)
  process.exit(2)
}
for (const f of [path.join(CERT_DIR, 'localhost.crt'), RAW_DIR, MM_LOG, ...LOG_FILES]) {
  if (!fs.existsSync(f)) { console.error(`Not found: ${f}`); process.exit(2) }
}
const certPem = fs.readFileSync(path.join(CERT_DIR, 'localhost.crt'))
// The worker's browser must trust exactly this certificate's key (KPI_TESTING=1 + KPI_TEST_TRUSTED_SPKI).
const SPKI = crypto.createHash('sha256')
  .update(new crypto.X509Certificate(certPem).publicKey.export({ type: 'spki', format: 'der' })).digest('base64')

const RUN = Date.now().toString(36)
const SRC = `E2E Logins ${RUN}`
const KEY_NAME = `E2E login ${RUN}`
const USER = `e2e-${RUN}@example.test`
const SECRET_PW = 'e2e-pw-' + crypto.randomBytes(12).toString('hex')
const AUTH = 'Basic ' + Buffer.from(`x:${PASSWORD}`).toString('base64')
const INBOX = fs.mkdtempSync(path.join(os.tmpdir(), 'kpi-e2e-logins-inbox-'))
const CODE_FILE = path.join(INBOX, 'code.txt') // the "email": the site writes each code here

// --- the HTTPS login site ------------------------------------------------------------------------------
const site = {
  codeMode: true, // ask for an emailed code after the password
  content: 'Plan A costs 10 EUR',
  sessions: new Set(), // valid session cookie values
  pending: new Map(), // pending token -> code
  issued: [], // every cookie value and code handed out, for the leak scan
  loginPosts: 0,
  codesSent: 0,
  codePosts: 0,
}
const LOGIN_FORM = (err = '') => `<html><body><h1>Sign in</h1>
<form method="post" action="/login">
  <input type="email" name="email"><input type="password" name="password">
  <button type="submit">Sign in</button>
</form>${err}</body></html>`
const VERIFY_FORM = (err = '') => `<html><body><p>We emailed you a code.</p>
<form method="post" action="/verify">
  <input type="text" name="otp" autocomplete="one-time-code" maxlength="32">
  <button type="submit">Verify</button>
</form>${err}</body></html>`
const cookies = (req) => Object.fromEntries((req.headers.cookie || '').split(/;\s*/).filter(Boolean)
  .map((c) => { const i = c.indexOf('='); return [c.slice(0, i), c.slice(i + 1)] }))
const html = (res, body, status = 200, headers = {}) => {
  res.writeHead(status, { 'content-type': 'text/html; charset=utf-8', ...headers })
  res.end(body)
}
const redirect = (res, to, headers = {}) => { res.writeHead(303, { location: to, ...headers }); res.end() }
const newToken = () => { const t = crypto.randomBytes(16).toString('hex'); site.issued.push(t); return t }
const readForm = (req) => new Promise((resolve) => {
  let b = ''
  req.on('data', (c) => { b += c })
  req.on('end', () => resolve(new URLSearchParams(b)))
})
const server = https.createServer({ cert: certPem, key: fs.readFileSync(path.join(CERT_DIR, 'localhost.key')) },
  async (req, res) => {
    const url = new URL(req.url, 'https://localhost')
    const c = cookies(req)
    if (url.pathname === '/robots.txt') { res.writeHead(404); return res.end() }
    if (url.pathname === '/login' && req.method === 'GET') return html(res, LOGIN_FORM())
    if (url.pathname === '/login' && req.method === 'POST') {
      site.loginPosts++
      const f = await readForm(req)
      if (f.get('email') !== USER || f.get('password') !== SECRET_PW) return html(res, LOGIN_FORM('<p>Wrong password</p>'))
      if (site.codeMode) {
        const token = newToken()
        const code = 'C' + crypto.randomBytes(8).toString('hex').toUpperCase()
        site.issued.push(code)
        site.pending.set(token, code)
        site.codesSent++
        fs.writeFileSync(CODE_FILE, code) // "send the email"
        return redirect(res, '/verify', { 'set-cookie': `pending=${token}; Path=/; Secure; HttpOnly` })
      }
      const sid = newToken()
      site.sessions.add(sid)
      return redirect(res, '/data', { 'set-cookie': `sid=${sid}; Path=/; Secure; HttpOnly` })
    }
    if (url.pathname === '/verify') {
      if (!site.pending.has(c.pending)) return redirect(res, '/login')
      if (req.method === 'GET') return html(res, VERIFY_FORM())
      site.codePosts++
      const f = await readForm(req)
      if (f.get('otp') !== site.pending.get(c.pending)) return html(res, VERIFY_FORM('<p>Wrong code</p>'))
      site.pending.delete(c.pending)
      const sid = newToken()
      site.sessions.add(sid)
      return redirect(res, '/data', { 'set-cookie': `sid=${sid}; Path=/; Secure; HttpOnly` })
    }
    if (url.pathname === '/data') {
      if (!site.sessions.has(c.sid)) { res.writeHead(302, { location: '/login' }); return res.end() }
      return html(res, `<html><body><h1>Members area</h1><p>${site.content}</p></body></html>`)
    }
    res.writeHead(404); res.end()
  }).listen(SITE_PORT, '127.0.0.1')

// --- helpers -------------------------------------------------------------------------------------------
const api = async (p, init = {}) => {
  const r = await fetch(BASE + p, {
    ...init,
    headers: { authorization: AUTH, ...(init.body ? { 'content-type': 'application/json' } : {}), ...(init.headers || {}) },
  })
  if (!r.ok) throw new Error(`${p} -> ${r.status} ${await r.text()}`)
  return r.status === 204 ? null : r.json()
}
const apiText = [] // every API response body, scanned for secrets at the end
const apiGet = async (p) => { const j = await api(p); apiText.push(JSON.stringify(j)); return j }

let cleaned = false
async function cleanup() {
  if (cleaned) return
  cleaned = true
  try {
    for (const s of await api('/api/custom-sources')) {
      if (s.name !== SRC) continue
      for (const t of s.targets) await api(`/api/watch-targets/${t.id}`, { method: 'DELETE' })
    }
  } catch (e) { console.error('cleanup failed (KPIs):', e.message) }
  try {
    const keys = await api('/api/credentials')
    for (const k of (Array.isArray(keys) ? keys : keys.items || [])) {
      if (k.name === KEY_NAME) await api(`/api/credentials/${k.id}`, { method: 'DELETE' })
    }
  } catch (e) { console.error('cleanup failed (login):', e.message) }
  fs.rmSync(INBOX, { recursive: true, force: true })
}
const withTimeout = (promise, ms) => Promise.race([promise, new Promise((r) => setTimeout(r, ms).unref())])
let browser
let closed = false
async function closeAll() {
  if (closed) return
  closed = true
  server.closeAllConnections?.()
  await withTimeout(Promise.allSettled([browser?.close(), new Promise((r) => server.close(() => r()))]), 5000)
}
for (const [sig, code] of [['SIGINT', 130], ['SIGTERM', 143]]) {
  process.on(sig, async () => {
    setTimeout(() => process.exit(code), 20000).unref()
    await withTimeout(cleanup(), 10000)
    await closeAll()
    process.exit(code)
  })
}
const step = (n, msg) => console.log(`[${n}] ${msg}`)
const shot = (page, name) => page.screenshot({ path: path.join(SHOTS, name), animations: 'disabled' })
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function finishedRuns(slug) {
  const { items } = await apiGet(`/api/runs?source=${slug}&limit=20`)
  return items.filter((r) => !['queued', 'running'].includes(r.status))
}
async function waitFinished(slug, before, timeoutMs = 120000) {
  const t0 = Date.now()
  while (Date.now() - t0 < timeoutMs) {
    const done = await finishedRuns(slug)
    if (done.length > before) return done[0]
    await sleep(2000)
  }
  throw new Error(`run of ${slug} did not finish in ${timeoutMs} ms`)
}
const runNow = (slug) => api(`/api/sources/${slug}/run`, { method: 'POST' })
const target = async () => (await apiGet('/api/custom-sources')).find((s) => s.name === SRC).targets[0]
const loginKey = async (id) => (await apiGet('/api/credentials')).find((k) => k.id === id)
// Mattermost "needs you to sign in" messages for this run's login.
const mmSignInMessages = () => fs.readFileSync(MM_LOG, 'utf8').split('\n')
  .filter((l) => l.includes(`${KEY_NAME} needs you to sign in`)).length

function walk(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? walk(path.join(dir, e.name)) : [path.join(dir, e.name)])
}
// Search every place a secret could leak into. Returns "where: which" strings.
function leakScan(secrets) {
  const hits = []
  const needles = secrets.flatMap((s) => [[s, s], [Buffer.from(s).toString('base64'), s]])
  const scanFile = (f) => {
    const buf = fs.readFileSync(f)
    for (const [n] of needles) if (buf.includes(n)) hits.push(f)
  }
  walk(RAW_DIR).forEach(scanFile)
  scanFile(MM_LOG)
  LOG_FILES.forEach(scanFile)
  const q = (sql) => execFileSync('psql', [PSQL_URL, '-At', '-c', sql], { encoding: 'utf8' }).trim()
  for (const [n] of needles) {
    const lit = "'%" + n.replace(/'/g, "''") + "%'"
    for (const [label, sql] of [
      ['runs', `select count(*) from runs r where r::text like ${lit}`],
      ['changes', `select count(*) from changes c where c::text like ${lit}`],
      ['watch_snapshots', `select count(*) from watch_snapshots s where s::text like ${lit}`],
      ['credentials', `select count(*) from credentials c where c::text like ${lit}`],
      ['watch_targets', `select count(*) from watch_targets t where t::text like ${lit}`],
      ['login_sessions', `select count(*) from login_sessions l where l::text like ${lit}`],
      ['login_attempts', `select count(*) from login_attempts a where a::text like ${lit}`],
    ]) if (Number(q(sql)) > 0) hits.push(label)
  }
  for (const t of apiText) for (const [n] of needles) if (t.includes(n)) hits.push('an API response')
  return [...new Set(hits)]
}

// --- the test ------------------------------------------------------------------------------------------
fs.mkdirSync(SHOTS, { recursive: true })
if (process.env.KPI_TEST_TRUSTED_SPKI && process.env.KPI_TEST_TRUSTED_SPKI !== SPKI) {
  console.error(`KPI_TEST_TRUSTED_SPKI does not match ${CERT_DIR}/localhost.crt (expected ${SPKI}).`)
  process.exit(2)
}
browser = await chromium.launch({
  executablePath: process.env.KPI_CHROMIUM_PATH || undefined,
  args: ['--no-sandbox'], // the container runs as root, where Chromium's sandbox cannot start
})
const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
const page = await ctx.newPage()
page.setDefaultTimeout(15000)

try {
  // 1. add a login key (API; the key form itself is covered by unit tests)
  const key = await api('/api/credentials', {
    method: 'POST',
    body: JSON.stringify({ name: KEY_NAME, kind: 'login', login_url: `https://localhost:${SITE_PORT}/login`, username: USER, secret: SECRET_PW }),
  })
  apiText.push(JSON.stringify(key))
  assert.equal(key.login_status, 'login_needed', 'a new login starts as "login needed"')
  step(1, `login "${KEY_NAME}" added (#${key.id}); status ${key.login_status}`)

  // 2. a KPI on the protected page; the API turns browser mode on by itself
  const t = await api('/api/custom-sources', {
    method: 'POST',
    body: JSON.stringify({ source_name: SRC, kpi_name: 'Members pricing', url: `https://localhost:${SITE_PORT}/data`, credential_id: key.id }),
  })
  apiText.push(JSON.stringify(t))
  assert.equal(t.use_browser, true, 'a KPI with a login is always fetched in browser mode')
  const slug = (await apiGet('/api/custom-sources')).find((s) => s.name === SRC).slug
  step(2, `KPI "${t.kpi_name}" added on ${t.url} (source ${slug}, use_browser=${t.use_browser})`)

  // 3. Run now: the worker signs in by itself, the site asks for a code -> login_needed + ONE message
  const mm0 = mmSignInMessages()
  await runNow(slug)
  const r1 = await waitFinished(slug, 0)
  let tg = await target()
  assert.equal(tg.last_status, 'login_needed', `KPI is "login needed" (got ${tg.last_status}: ${tg.last_error})`)
  assert.equal(site.loginPosts, 1, 'the worker tried the password once')
  assert.equal(site.codePosts, 0, 'nobody typed a code')
  assert.ok(fs.existsSync(CODE_FILE), 'the site "emailed" a code')
  let lk = await loginKey(key.id)
  assert.equal(lk.login_status, 'login_needed', 'the login itself is "login needed"')
  assert.equal(mmSignInMessages() - mm0, 1, 'exactly one Mattermost "needs you to sign in" message')
  step(3, `Run now -> run #${r1.id} status=${r1.status} (${r1.error}); KPI login_needed; login error: ${lk.login_error}; 1 Mattermost message`)
  // a second run while still waiting: no new sign-in, no new code email, no second message
  await runNow(slug)
  const r1b = await waitFinished(slug, 1)
  assert.equal(site.loginPosts, 1, 'no automatic retry once the site wants a code')
  assert.equal(mmSignInMessages() - mm0, 1, 'still exactly one Mattermost message')
  assert.equal((await target()).last_status, 'login_needed')
  step(3, `second run #${r1b.id} status=${r1b.status}: no new sign-in, still one message`)

  // 4. Sign in now in the dashboard; read the code from the "inbox" and type it into the dialog
  fs.rmSync(CODE_FILE)
  const before4 = (await finishedRuns(slug)).length
  await page.goto(BASE + '/login')
  await page.fill('#pw', PASSWORD)
  await page.click('button[type=submit]')
  await page.waitForURL(BASE + '/')
  await page.locator('li.source', { hasText: SRC }).locator('[data-test=signin-needed]').waitFor()
  await page.goto(BASE + '/settings#logins')
  await page.locator(`[data-test=key-${key.id}]`).getByText('Login needed').waitFor()
  await page.locator(`[data-test=signin-${key.id}]`).click()
  const dialog = page.getByRole('dialog')
  await dialog.locator('#si-code').waitFor({ timeout: 90000 })
  await dialog.locator('[data-test=countdown]').waitFor()
  assert.ok(fs.existsSync(CODE_FILE), 'the site emailed a new code')
  const code = fs.readFileSync(CODE_FILE, 'utf8').trim()
  await shot(page, 'logins-code.png')
  await dialog.locator('#si-code').fill(code)
  await dialog.locator('[data-test=code-submit]').click()
  await dialog.locator('[data-test=succeeded]').waitFor({ timeout: 90000 })
  assert.ok(!(await page.content()).includes(code), 'the code is not left on the page')
  assert.equal(site.codePosts, 1, 'the worker typed the code once')
  await shot(page, 'logins-signed-in.png')
  const att = await apiGet(`/api/logins/${key.id}/attempt`)
  assert.equal(att.status, 'succeeded')
  const r2 = await waitFinished(slug, before4)
  assert.equal(r2.status, 'baseline', `the KPI is checked automatically, first as a baseline (got ${r2.status}: ${r2.error})`)
  tg = await target()
  assert.equal(tg.last_status, 'ok', `KPI is ok after signing in (got ${tg.last_status}: ${tg.last_error})`)
  assert.equal((await loginKey(key.id)).login_status, 'ok')
  await dialog.locator('[data-test=close]').click()
  await page.locator(`[data-test=key-${key.id}] [data-test=login-state]`).getByText('Signed in').waitFor({ timeout: 20000 })
  step(4, `Sign in now -> dialog asked for the code; entered it -> succeeded; run #${r2.id} status=${r2.status}; KPI ok`)

  // 5. the protected page changes -> ok with an excerpt, using the saved session (no new sign-in)
  site.content = 'Plan A costs 12 EUR'
  const posts5 = site.loginPosts
  await runNow(slug)
  const r3 = await waitFinished(slug, before4 + 1)
  assert.equal(r3.status, 'ok', `the change is reported (got ${r3.status}: ${r3.error})`)
  assert.equal(site.loginPosts, posts5, 'the saved session was reused, no new sign-in')
  const ch = await apiGet(`/api/runs/${r3.id}/changes`)
  const ex = JSON.stringify(ch.items.map((i) => i.excerpt))
  assert.ok(ex.includes('12 EUR'), 'the excerpt shows the new text: ' + ex)
  await page.goto(BASE + `/runs/${r3.id}`)
  const exText = await page.locator('[data-test=excerpt]').first().innerText()
  assert.match(exText, /\+ .*12 EUR/, 'run detail shows the + line')
  step(5, `page changed -> run #${r3.id} status=${r3.status}; excerpt: ${exText.replace(/\n/g, ' / ')}`)

  // 6. the site drops every session and stops asking for codes -> the next run signs in again by itself
  site.sessions.clear()
  site.codeMode = false
  site.content = 'Plan A costs 14 EUR'
  const posts6 = site.loginPosts
  const codes6 = site.codesSent
  const mm6 = mmSignInMessages()
  await runNow(slug)
  const r4 = await waitFinished(slug, before4 + 2)
  assert.equal(r4.status, 'ok', `checked after signing in again (got ${r4.status}: ${r4.error})`)
  assert.equal(site.loginPosts, posts6 + 1, 'the worker signed in again by itself, once')
  assert.equal(site.codesSent, codes6, 'no code was asked for')
  assert.equal(mmSignInMessages(), mm6, 'no "needs you to sign in" message')
  tg = await target()
  assert.equal(tg.last_status, 'ok')
  assert.equal((await loginKey(key.id)).login_status, 'ok')
  step(6, `session expired, no code -> run #${r4.id} status=${r4.status}; signed in again by itself; no message`)

  // 7. nothing leaked: password, every code and every session cookie value
  apiText.push(JSON.stringify([await api('/api/credentials'), await api('/api/custom-sources'), await api('/api/runs?limit=50')]))
  const secrets = [SECRET_PW, ...site.issued]
  const hits = leakScan(secrets)
  assert.deepEqual(hits, [], 'secret found in: ' + hits.join(', '))
  step(7, `no password, code or cookie (${secrets.length} secrets) in: database, raw files, Mattermost bodies, logs, API responses`)

  step('done', 'ALL PASSED; test KPI and login cleaned up')
} catch (e) {
  console.error('FAILED:', e)
  await page.screenshot({ path: path.join(SHOTS, 'e2e-logins-failure.png') }).catch(() => {})
  console.error('failure screenshot in ' + SHOTS)
  process.exitCode = 1
} finally {
  // 8. clean up
  await cleanup()
  await closeAll()
}
