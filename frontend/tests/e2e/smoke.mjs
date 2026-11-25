// End-to-end smoke test against a running stack (api :8000, worker, `vite preview` :8080).
// Run: npm run e2e   (see README, "End-to-end test"). Playwright comes from NODE_PATH / global install.
import { createRequire } from 'node:module'
import http from 'node:http'
import fs from 'node:fs'
import path from 'node:path'
import assert from 'node:assert/strict'

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')

const BASE = process.env.E2E_BASE_URL || 'http://localhost:8080'
const PASSWORD = process.env.DASHBOARD_PASSWORD || 'dev'
const PAGE_PORT = Number(process.env.E2E_PAGE_PORT || 8765)
const SHOTS = process.env.E2E_SCREENSHOTS || path.resolve(import.meta.dirname, '../../../docs/screenshots')
const MM_LOG = process.env.E2E_MM_LOG // optional Mattermost stub log, printed at the end
const RUN = Date.now().toString(36)
const SRC = `E2E Pages ${RUN}`
const IMP = `E2E Imports ${RUN}`
const AUTH = 'Basic ' + Buffer.from(`x:${PASSWORD}`).toString('base64')

let html = '<html><body><h1>Pricing</h1><p>Plan A costs 10 EUR</p></body></html>'
const server = http.createServer((req, res) => {
  if (req.url === '/robots.txt') { res.writeHead(404); return res.end() }
  res.writeHead(200, { 'content-type': 'text/html' })
  res.end(html)
}).listen(PAGE_PORT, '127.0.0.1')

const api = async (p, init = {}) => {
  const r = await fetch(BASE + p, { ...init, headers: { authorization: AUTH, ...(init.headers || {}) } })
  if (!r.ok) throw new Error(`${p} -> ${r.status}`)
  return r.status === 204 ? null : r.json()
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

fs.mkdirSync(SHOTS, { recursive: true })
const browser = await chromium.launch({ executablePath: process.env.KPI_CHROMIUM_PATH || undefined, args: ['--no-sandbox'] })
const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
const page = await ctx.newPage()
page.setDefaultTimeout(15000)
let slug

try {
  // 1. login
  await page.goto(BASE + '/login')
  await page.fill('#pw', 'wrong-password')
  await page.click('button[type=submit]')
  await page.getByRole('alert').waitFor()
  step(1, 'wrong password -> error: ' + (await page.getByRole('alert').innerText()))
  await page.fill('#pw', PASSWORD)
  await page.click('button[type=submit]')
  await page.waitForURL(BASE + '/')
  await page.getByRole('heading', { name: 'Overview' }).waitFor()
  await page.locator('[data-test=source-name]').first().waitFor()
  const names = await page.locator('[data-test=source-name]').allInnerTexts()
  for (const n of ['World Bank', 'IMF', 'SIPRI']) assert(names.some((x) => x.includes(n)), `overview lists ${n}`)
  step(1, 'logged in; Overview lists: ' + names.join(' | '))
  await page.getByRole('button', { name: 'Light', exact: true }).click()
  await page.waitForTimeout(300)
  await shot(page, 'overview-light.png')
  await page.getByRole('button', { name: 'Dark', exact: true }).click()
  await page.waitForTimeout(300)
  await shot(page, 'overview-dark.png')
  await page.getByRole('button', { name: 'Light', exact: true }).click()

  // 2. changes + run detail (the page-change run is opened at step 6)
  await page.goto(BASE + '/changes')
  await page.locator('.feed tbody tr').first().waitFor()
  const nRuns = await page.locator('.feed tbody tr').count()
  assert(nRuns >= 1)
  await page.locator('.feed tbody tr a').first().click()
  await page.getByRole('heading', { name: /Run #\d+/ }).waitFor()
  step(2, `Changes lists ${nRuns} run(s); first run detail renders`)

  // 3. explorer
  await page.goto(BASE + '/explorer')
  await page.getByRole('link', { name: /SIPRI/ }).click()
  await page.getByRole('link', { name: /constant/i }).first().click()
  await page.fill('#ex-q', 'France')
  await page.getByRole('link', { name: 'France', exact: true }).first().click()
  await page.locator('[data-test=chart] canvas, canvas').first().waitFor({ state: 'visible' })
  await page.waitForTimeout(800)
  await shot(page, 'explorer-chart.png')
  step(3, 'Explorer sipri-milex -> constant-usd -> France: chart canvas visible')

  // 4. KPI frequency
  await page.goto(BASE + '/kpis')
  await page.locator('.kpis tbody tr').first().waitFor()
  const nk = await page.locator('.kpis tbody tr').count()
  assert(nk > 0)
  await page.waitForTimeout(500)
  await shot(page, 'kpi-frequency.png')
  step(4, `KPI frequency loads ${nk} rows`)

  // 5. custom KPI
  await page.goto(BASE + '/settings')
  await page.locator('[data-test=add]').click()
  await page.fill('#tf-source', SRC)
  await page.fill('#tf-kpi', 'Pricing')
  await page.fill('#tf-url', `http://127.0.0.1:${PAGE_PORT}/page.html`)
  await page.locator('[data-test=test-link]').click()
  const prev = page.locator('[data-test=test-result]')
  await prev.waitFor()
  assert((await prev.innerText()).includes('Plan A costs 10 EUR'), 'preview shows page text')
  step(5, 'Test link preview contains "Plan A costs 10 EUR"')
  await page.getByRole('button', { name: 'Add KPI', exact: true }).last().click()
  await page.getByRole('region', { name: SRC }).waitFor()
  step(5, 'saved; appears under custom source ' + SRC)
  slug = (await api('/api/custom-sources')).find((s) => s.name === SRC).slug
  await page.goto(BASE + '/')
  const card = page.locator('li.source', { hasText: SRC })
  await card.waitFor()
  await card.getByRole('button', { name: 'Run now' }).click()
  const r1 = await waitFinished(slug, 0)
  assert.equal(r1.status, 'baseline', 'first check is a silent baseline')
  step(5, `Run now -> run #${r1.id} status=${r1.status} (silent baseline)`)
  await page.goto(BASE + '/settings')
  await page.getByRole('region', { name: SRC }).getByText('OK', { exact: false }).first().waitFor().catch(() => {})

  // 6. change the page, run again
  html = '<html><body><h1>Pricing</h1><p>Plan A costs 12 EUR</p></body></html>'
  await page.goto(BASE + '/')
  const card2 = page.locator('li.source', { hasText: SRC })
  await card2.getByRole('button', { name: 'Run now' }).click()
  const r2 = await waitFinished(slug, 1)
  assert.equal(r2.status, 'ok', 'second check reports the change')
  await page.goto(BASE + '/changes?changes=0')
  await page.locator(`a[href="/runs/${r2.id}"]`).first().waitFor()
  await page.goto(BASE + `/runs/${r2.id}`)
  const ex = page.locator('[data-test=excerpt]').first()
  await ex.waitFor()
  const exText = await ex.innerText()
  assert(/\+ .*12 EUR/.test(exText), 'excerpt has + line: ' + exText)
  await page.waitForTimeout(300)
  await shot(page, 'run-detail.png')
  step(6, `edited page -> run #${r2.id} status=${r2.status}; detail excerpt: ${exText.replace(/\n/g, ' / ')}`)

  // 7. import
  const csv = [
    'Source name,KPI name,Link',
    `${IMP},Alpha,http://127.0.0.1:${PAGE_PORT}/a.html`,
    `${IMP},Beta,http://127.0.0.1:${PAGE_PORT}/b.html`,
    `${SRC},Pricing,http://127.0.0.1:${PAGE_PORT}/page.html`,
  ].join('\r\n') + '\r\n'
  const csvPath = path.join(SHOTS, '..', `.e2e-import-${RUN}.csv`)
  fs.writeFileSync(csvPath, csv)
  await page.goto(BASE + '/settings')
  await page.locator('[data-test=import]').click()
  await page.setInputFiles('#imp-file', csvPath)
  const dlg = page.getByRole('dialog')
  await dlg.getByText('to import').waitFor()
  const sum = await dlg.getByRole('status').first().innerText()
  const chips = await dlg.locator('.chip').allInnerTexts()
  assert(chips.filter((c) => /Duplicate/.test(c)).length === 1 && chips.filter((c) => /New source/.test(c)).length === 2, chips.join(','))
  step(7, `preview: "${sum.trim()}"; statuses: ${chips.map((c) => c.trim()).join(' | ')}`)
  await dlg.getByRole('button', { name: /^Import 2 KPIs/ }).click()
  await dlg.getByText('Import finished').waitFor()
  const res = await dlg.getByText(/Created/).innerText()
  assert(/Created 2 KPIs/.test(res), res)
  step(7, 'confirm: ' + res.replace(/\s+/g, ' '))
  await dlg.getByRole('button', { name: 'Close' }).click()
  fs.unlinkSync(csvPath)

  // 8. delete one imported KPI (screenshot first, so Settings shows both groups)
  await page.getByRole('region', { name: IMP }).waitFor()
  await page.locator('[data-test=add]').scrollIntoViewIfNeeded()
  await page.waitForTimeout(400)
  await shot(page, 'settings-custom-sources.png')
  const alphaRow = page.getByRole('region', { name: IMP }).locator('tr', { hasText: 'Alpha' })
  await alphaRow.getByRole('button', { name: 'Delete' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click()
  await alphaRow.waitFor({ state: 'detached' })
  assert.equal(await page.getByRole('region', { name: IMP }).locator('tbody tr').count(), 1)
  step(8, 'deleted imported KPI "Alpha"; "Beta" remains')

  // cleanup of everything this run created
  for (const s of await api('/api/custom-sources')) {
    if (![SRC, IMP].includes(s.name)) continue
    for (const t of s.targets) await api(`/api/watch-targets/${t.id}`, { method: 'DELETE' })
  }
  step('done', 'ALL PASSED; test KPIs cleaned up')
} catch (e) {
  console.error('FAILED:', e)
  await page.screenshot({ path: path.join(SHOTS, '..', 'e2e-failure.png') }).catch(() => {})
  process.exitCode = 1
} finally {
  if (MM_LOG && fs.existsSync(MM_LOG)) console.log('Mattermost messages received:\n' + fs.readFileSync(MM_LOG, 'utf8'))
  await browser.close()
  server.close()
}
