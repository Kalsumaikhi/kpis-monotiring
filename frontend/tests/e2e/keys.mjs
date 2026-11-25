// End-to-end test of Keys against a running stack (api :8000, worker, `vite preview` :8080).
// Run: npm run e2e:keys   (see README, "Keys end-to-end test"). Playwright comes from NODE_PATH / global install.
// The test serves a local HTTPS page that answers 200 only when it sees the expected header, then checks
// that neither secret leaks into the database, raw files, Mattermost messages or process logs.
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
const PAGE_PORT = Number(process.env.E2E_KEYS_PORT || 8443)
const CERT_DIR = process.env.E2E_KEYS_CERT_DIR // holds localhost.crt + localhost.key (see README)
const RAW_DIR = process.env.RAW_DIR // optional: scanned for the secrets
const MM_LOG = process.env.E2E_MM_LOG // optional: file the Mattermost stub appends bodies to
const LOG_FILES = (process.env.E2E_LOG_FILES || '').split(',').filter(Boolean) // optional: api/worker logs
const PSQL_URL = process.env.E2E_PSQL_URL // optional: plain postgresql:// URL, scanned with psql
const SHOTS = process.env.E2E_SCREENSHOTS || fs.mkdtempSync(path.join(os.tmpdir(), 'kpi-e2e-keys-shots-'))
if (!CERT_DIR) {
  console.error('Set E2E_KEYS_CERT_DIR to a folder with localhost.crt and localhost.key (README, "Keys end-to-end test").')
  process.exit(2)
}
const RUN = Date.now().toString(36)
const SRC = `E2E Keys ${RUN}`
const KEY_NAME = `E2E key ${RUN}`
const HEADER = 'X-Api-Key'
const SECRET_A = 'e2e-good-' + crypto.randomBytes(12).toString('hex')
const SECRET_B = 'e2e-wrong-' + crypto.randomBytes(12).toString('hex')
const AUTH = 'Basic ' + Buffer.from(`x:${PASSWORD}`).toString('base64')

// The HTTPS stub: 200 with a page only when the right header value arrives, else 401. Remembers what it saw.
let accepted = SECRET_A
const seen = []
const server = https.createServer({
  cert: fs.readFileSync(path.join(CERT_DIR, 'localhost.crt')),
  key: fs.readFileSync(path.join(CERT_DIR, 'localhost.key')),
}, (req, res) => {
  if (req.url === '/robots.txt') { res.writeHead(404); return res.end() }
  if (req.url === '/public.html') { // a keyless KPI, so the run can end "incomplete" instead of failing outright
    res.writeHead(200, { 'content-type': 'text/html' })
    return res.end('<html><body><h1>Public</h1><p>Open data 7</p></body></html>')
  }
  const got = req.headers[HEADER.toLowerCase()]
  seen.push(got ?? null)
  if (got !== accepted) { res.writeHead(401); return res.end('unauthorized') }
  res.writeHead(200, { 'content-type': 'text/html' })
  res.end('<html><body><h1>Private</h1><p>Plan A costs 10 EUR</p></body></html>')
}).listen(PAGE_PORT, '127.0.0.1')

const api = async (p, init = {}) => {
  const r = await fetch(BASE + p, { ...init, headers: { authorization: AUTH, ...(init.headers || {}) } })
  if (!r.ok) throw new Error(`${p} -> ${r.status}`)
  return r.status === 204 ? null : r.json()
}
// Remove the KPI, the (now empty) source and the key this run created. Runs on success, failure and signals.
async function cleanup() {
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
  } catch (e) { console.error('cleanup failed (key):', e.message) }
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

async function waitFinished(slug, before, timeoutMs = 90000) {
  const t0 = Date.now()
  while (Date.now() - t0 < timeoutMs) {
    const { items } = await api(`/api/runs?source=${slug}&limit=20`)
    const done = items.filter((r) => !['queued', 'running'].includes(r.status))
    if (done.length > before) return done[0]
    await new Promise((r) => setTimeout(r, 2000))
  }
  throw new Error(`run of ${slug} did not finish in ${timeoutMs} ms`)
}

// Search every place a secret could leak into. Returns a list of "where" strings that contain it.
function walk(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? walk(path.join(dir, e.name)) : [path.join(dir, e.name)])
}
function leakScan(secrets) {
  const hits = []
  const b64 = secrets.map((s) => Buffer.from(s).toString('base64'))
  const needles = [...secrets, ...b64]
  const scanFile = (f) => {
    const buf = fs.readFileSync(f)
    for (const n of needles) if (buf.includes(n)) hits.push(f)
  }
  if (RAW_DIR && fs.existsSync(RAW_DIR)) walk(RAW_DIR).forEach(scanFile)
  if (MM_LOG && fs.existsSync(MM_LOG)) scanFile(MM_LOG)
  LOG_FILES.filter((f) => fs.existsSync(f)).forEach(scanFile)
  if (PSQL_URL) {
    const q = (sql) => execFileSync('psql', [PSQL_URL, '-At', '-c', sql], { encoding: 'utf8' }).trim()
    for (const n of needles) {
      const lit = "'%" + n.replace(/'/g, "''") + "%'"
      for (const [label, sql] of [
        ['runs.error', `select count(*) from runs where error like ${lit}`],
        ['watch_snapshots.content', `select count(*) from watch_snapshots where content::text like ${lit}`],
        ['credentials (any column)', `select count(*) from credentials c where c::text like ${lit}`],
        ['watch_targets', `select count(*) from watch_targets t where t::text like ${lit}`],
      ]) if (Number(q(sql)) > 0) hits.push(label)
    }
  }
  return hits
}

fs.mkdirSync(SHOTS, { recursive: true })
browser = await chromium.launch({
  executablePath: process.env.KPI_CHROMIUM_PATH || undefined,
  args: ['--no-sandbox'], // the container runs as root, where Chromium's sandbox cannot start
})
const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
const page = await ctx.newPage()
page.setDefaultTimeout(15000)
let slug

try {
  await page.goto(BASE + '/login')
  await page.fill('#pw', PASSWORD)
  await page.click('button[type=submit]')
  await page.waitForURL(BASE + '/')
  step(1, 'logged in')

  // 2. add a header key in the UI
  await page.goto(BASE + '/settings')
  await page.locator('[data-test=add-key]').click()
  await page.fill('#kf-name', KEY_NAME)
  await page.fill('#kf-header', HEADER)
  await page.fill('#kf-secret', SECRET_A)
  await page.getByRole('button', { name: 'Add key', exact: true }).last().click()
  await page.locator('.item', { hasText: KEY_NAME }).getByText('Set').waitFor()
  assert.equal(await page.locator('#kf-secret').count(), 0, 'form closed and the secret field is gone')
  assert.ok(!(await page.content()).includes(SECRET_A), 'secret is not in the page after saving')
  step(2, `key "${KEY_NAME}" added (header ${HEADER}); card shows Set, no secret on the page`)

  // 3. add a KPI that uses the key, test the link
  await page.locator('[data-test=add]').click()
  await page.fill('#tf-source', SRC)
  await page.fill('#tf-kpi', 'Private pricing')
  await page.fill('#tf-url', `https://localhost:${PAGE_PORT}/private.html`)
  await page.selectOption('#tf-key', { label: KEY_NAME })
  await page.locator('[data-test=test-link]').click()
  const prev = page.locator('[data-test=test-result]')
  await prev.waitFor()
  assert((await prev.innerText()).includes('Plan A costs 10 EUR'), 'preview shows the private page text')
  assert(seen.includes(SECRET_A), 'the stub saw the key header on Test link')
  step(3, 'Test link OK; preview shows the private page; stub saw the header')
  await page.getByRole('button', { name: 'Add KPI', exact: true }).last().click()
  await page.getByRole('region', { name: SRC }).waitFor()
  // a second KPI in the same source with no key
  await page.locator('[data-test=add]').click()
  await page.fill('#tf-source', SRC)
  await page.fill('#tf-kpi', 'Public data')
  await page.fill('#tf-url', `https://localhost:${PAGE_PORT}/public.html`)
  await page.getByRole('button', { name: 'Add KPI', exact: true }).last().click()
  await page.getByRole('region', { name: SRC }).locator('tbody tr', { hasText: 'Public data' }).waitFor()
  slug = (await api('/api/custom-sources')).find((s) => s.name === SRC).slug

  // 4. Run now -> baseline
  await page.goto(BASE + '/')
  await page.locator('li.source', { hasText: SRC }).getByRole('button', { name: 'Run now' }).click()
  const r1 = await waitFinished(slug, 0)
  assert.equal(r1.status, 'baseline', `first check is a silent baseline (got ${r1.status}: ${r1.error})`)
  const seenBefore = seen.length
  assert(seen.slice(0, seenBefore).filter((h) => h === SECRET_A).length >= 2, 'the worker request carried the header')
  step(4, `Run now -> run #${r1.id} status=${r1.status}; stub saw the header on the worker fetch`)
  await page.goto(BASE + '/settings')
  await page.getByRole('region', { name: SRC }).locator('tbody tr', { hasText: 'Private pricing' }).getByText('OK', { exact: true }).waitFor({ timeout: 20000 })
  await shot(page, 'keys-ok.png')

  // 5. replace the secret with a wrong value; the stub still wants SECRET_A
  await page.locator('.item', { hasText: KEY_NAME }).getByRole('button', { name: 'Replace secret' }).click()
  await page.fill('#kf-secret', SECRET_B)
  await page.locator('form[aria-label]').getByRole('button', { name: 'Replace secret', exact: true }).click()
  await page.locator('#kf-secret').waitFor({ state: 'detached' })
  await page.goto(BASE + '/')
  await page.locator('li.source', { hasText: SRC }).getByRole('button', { name: 'Run now' }).click()
  const r2 = await waitFinished(slug, 1)
  assert.ok(seen.includes(SECRET_B), 'the stub received the wrong secret')
  assert.match(r2.error || '', /^incomplete:/, 'run is flagged incomplete: ' + r2.error)
  assert.ok((r2.error || '').includes('Private pricing'), 'the incomplete warning names the KPI: ' + r2.error)
  step(5, `wrong secret -> run #${r2.id} status=${r2.status}, error: ${r2.error}`)
  await page.goto(BASE + '/settings')
  await page.getByRole('region', { name: SRC }).locator('tbody tr', { hasText: 'Private pricing' }).getByText('Key rejected', { exact: true }).waitFor({ timeout: 20000 })
  await shot(page, 'keys-rejected.png')
  step(5, 'Settings shows "Key rejected" on the KPI')

  // 6. nothing leaked anywhere
  const hits = leakScan([SECRET_A, SECRET_B])
  assert.deepEqual(hits, [], 'secret found in: ' + hits.join(', '))
  const scanned = ['api responses (credentials, runs, targets)', PSQL_URL && 'database', RAW_DIR && 'raw files', MM_LOG && 'Mattermost bodies', LOG_FILES.length && 'logs'].filter(Boolean)
  const apiText = JSON.stringify([await api('/api/credentials'), await api('/api/custom-sources'), await api('/api/runs?limit=50')])
  assert.ok(!apiText.includes(SECRET_A) && !apiText.includes(SECRET_B), 'secret in an API response')
  step(6, 'no secret found in: ' + scanned.join(', '))

  step('done', 'ALL PASSED; test KPI, source and key cleaned up')
} catch (e) {
  console.error('FAILED:', e)
  await page.screenshot({ path: path.join(SHOTS, 'e2e-keys-failure.png') }).catch(() => {})
  console.error('failure screenshot in ' + SHOTS)
  process.exitCode = 1
} finally {
  await cleanup()
  await closeAll()
}
