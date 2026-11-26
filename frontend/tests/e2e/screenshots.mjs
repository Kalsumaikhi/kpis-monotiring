// Regenerates docs/screenshots/*.png against a running stack (same setup as smoke.mjs).
// Creates a demo custom source "Example pages" with two public-page KPIs, plus ONE local
// KPI (127.0.0.1) that is edited between two runs to produce a page-change excerpt for the
// run-detail shot; that local KPI is deleted before the Settings/Overview shots.
// 'dev' is a TEST-ONLY password for a throwaway local stack.
import { createRequire } from 'node:module'
import http from 'node:http'
import fs from 'node:fs'
import path from 'node:path'

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const BASE = process.env.E2E_BASE_URL || 'http://localhost:8080'
const PASSWORD = process.env.DASHBOARD_PASSWORD || 'dev'
const PORT = Number(process.env.E2E_PAGE_PORT || 8765)
const OUT = path.resolve(import.meta.dirname, '../../../docs/screenshots')
const AUTH = 'Basic ' + Buffer.from(`x:${PASSWORD}`).toString('base64')
const SRC = 'Example pages'

let html = '<html><body><h1>Pricing</h1><p>Plan A costs 10 EUR</p></body></html>'
const server = http.createServer((q, r) => { r.writeHead(q.url === '/robots.txt' ? 404 : 200, { 'content-type': 'text/html' }); r.end(q.url === '/robots.txt' ? '' : html) }).listen(PORT, '127.0.0.1')

const api = async (p, method = 'GET', body) => {
  const r = await fetch(BASE + p, { method, headers: { authorization: AUTH, 'x-requested-with': 'kpi-monitor', ...(body ? { 'content-type': 'application/json' } : {}) }, body: body && JSON.stringify(body) })
  if (!r.ok) throw new Error(`${method} ${p} -> ${r.status} ${await r.text()}`)
  return r.status === 204 ? null : r.json()
}
const finished = async (slug) => (await api(`/api/runs?source=${slug}&limit=50`)).items.filter((r) => !['queued', 'running'].includes(r.status))
async function runAndWait(slug) {
  const before = (await finished(slug)).length
  await api(`/api/sources/${slug}/run`, 'POST')
  for (let i = 0; i < 45; i++) {
    const d = await finished(slug)
    if (d.length > before) return d[0]
    await new Promise((r) => setTimeout(r, 2000))
  }
  throw new Error('run did not finish')
}

fs.mkdirSync(OUT, { recursive: true })
const browser = await chromium.launch({ args: ['--no-sandbox'] })
const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
const page = await ctx.newPage()
const shot = async (name) => { await page.waitForTimeout(600); await page.screenshot({ path: path.join(OUT, name), animations: 'disabled' }) }
let localId
try {
  const have = (await api('/api/custom-sources')).find((s) => s.name === SRC)
  const names = new Set((have?.targets ?? []).map((t) => t.kpi_name))
  const add = (kpi, url) => names.has(kpi) ? null : api('/api/custom-sources', 'POST', { source_name: SRC, kpi_name: kpi, url, kind: 'auto', selector: null, use_browser: false })
  await add('Example Domain', 'https://example.com')
  await add('IANA example domains', 'https://www.iana.org/help/example-domains')
  await add('Pricing demo', `http://127.0.0.1:${PORT}/page.html`)
  const src = (await api('/api/custom-sources')).find((s) => s.name === SRC)
  localId = src.targets.find((t) => t.kpi_name === 'Pricing demo').id
  const r1 = await runAndWait(src.slug)
  html = '<html><body><h1>Pricing</h1><p>Plan A costs 12 EUR</p></body></html>'
  const r2 = await runAndWait(src.slug)
  console.log(`runs: #${r1.id} ${r1.status}, #${r2.id} ${r2.status}`)

  await page.goto(BASE + '/login')
  await page.fill('#pw', PASSWORD)
  await page.click('button[type=submit]')
  await page.waitForURL(BASE + '/')
  await page.getByRole('button', { name: 'Light', exact: true }).click()

  await page.goto(BASE + `/runs/${r2.id}`)
  await page.locator('[data-test=excerpt]').first().waitFor()
  await shot('run-detail.png')

  await api(`/api/watch-targets/${localId}`, 'DELETE'); localId = null // drop the 127.0.0.1 KPI
  await page.goto(BASE + '/')
  await page.locator('li.source', { hasText: SRC }).waitFor()
  await shot('overview-light.png')
  await page.getByRole('button', { name: 'Dark', exact: true }).click()
  await shot('overview-dark.png')
  await page.getByRole('button', { name: 'Light', exact: true }).click()

  await page.goto(BASE + '/kpis')
  await page.locator('.kpis tbody tr').first().waitFor()
  await shot('kpi-frequency.png')

  await page.goto(BASE + '/explorer')
  await page.getByRole('link', { name: /SIPRI/ }).click()
  await page.getByRole('link', { name: /constant/i }).first().click()
  await page.fill('#ex-q', 'France')
  await page.getByRole('link', { name: 'France', exact: true }).first().click()
  await page.locator('canvas').first().waitFor({ state: 'visible' })
  await shot('explorer-chart.png')

  await page.goto(BASE + '/settings')
  await page.getByRole('region', { name: SRC }).waitFor()
  await page.getByRole('region', { name: SRC }).scrollIntoViewIfNeeded()
  await shot('settings-custom-sources.png')
} finally {
  if (localId) await api(`/api/watch-targets/${localId}`, 'DELETE').catch(() => {})
  await browser.close()
  server.close()
}
