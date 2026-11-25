import { beforeEach, describe, expect, it, vi } from 'vitest'
import { h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'

const apiGet = vi.fn()
vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, apiGet: (...a: unknown[]) => apiGet(...a) }
})
vi.mock('../components/SeriesChart.vue', () => ({ default: { props: ['series', 'labels', 'label'], render(this: { series: unknown }) { return h('div', { 'data-series': JSON.stringify(this.series) }) } } }))
import ExplorerPage from '../pages/ExplorerPage.vue'
import { isNumericSeries, sortPeriods } from '../lib/series'

const pt = (period: string, value: string | null, dims: Record<string, unknown> | null = null) => ({ period, dims, value })

async function mountAt(path: string, handler: (url: string, params?: Record<string, unknown>) => unknown) {
  apiGet.mockImplementation(async (url: string, params?: Record<string, unknown>) => handler(url, params))
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/explorer/:slug?/:indicator?/:country?', name: 'explorer', component: ExplorerPage }],
  })
  router.push(path)
  await router.isReady()
  const w = mount(ExplorerPage, { global: { plugins: [router] } })
  await flushPromises()
  await flushPromises()
  return { w, router }
}
beforeEach(() => { apiGet.mockReset() })

const sources = [{ slug: 's', name: 'Src', area: 'x', rows_tracked: 10 }]

describe('series helpers', () => {
  it('detects numeric series and sorts periods', () => {
    expect(isNumericSeries([pt('2020', '1'), pt('2021', null)])).toBe(true)
    expect(isNumericSeries([pt('2020', '1'), pt('2021', 'n/a')])).toBe(false)
    expect(sortPeriods(['2024', '999', '2020'])).toEqual(['999', '2020', '2024'])
    expect(sortPeriods(['2024-Q3', '2024-Q1'])).toEqual(['2024-Q1', '2024-Q3'])
    expect(sortPeriods(['b', '2020', 'a'])).toEqual(['b', '2020', 'a'])
  })
})

describe('Explorer', () => {
  it('explorer shows table only for text values', async () => {
    const { w } = await mountAt('/explorer/s/I/DE', (url) => {
      if (url === '/api/sources') return sources
      return { points: [pt('2024', 'Active'), pt('2025', 'Paused')], history: [], attribution: 'Data: Src' }
    })
    expect(w.find('[data-test=chart]').exists()).toBe(false)
    expect(w.find('[data-test=text-only]').exists()).toBe(true)
    expect(w.text()).toContain('Active')
    expect(w.text()).toContain('Data: Src')
  })

  it('numeric values get a chart, history rows and a run link', async () => {
    const { w } = await mountAt('/explorer/s/I/DE', (url) => {
      if (url === '/api/sources') return sources
      return {
        points: [pt('2024', '10000'), pt('2025', '12')],
        history: [{ run_id: 7, created_at: '2026-11-01T00:00:00Z', change_type: 'altered', period: '2025', dims: null, old: '11', new: '12' }],
        attribution: null,
      }
    })
    expect(w.find('[data-test=chart]').exists()).toBe(true)
    expect(w.text()).toContain('10,000')
    expect(w.text()).toContain('11 → 12')
    expect(w.get('a[href="/runs/7"]').text()).toContain('Run #7')
  })

  it('more than 6 dims sets shows the note', async () => {
    const points = Array.from({ length: 8 }, (_, i) => pt('2024', String(i), { k: i }))
    const { w } = await mountAt('/explorer/s/I/DE', (url) => (url === '/api/sources' ? sources : { points, history: [], attribution: null }))
    expect(w.get('[data-test=series-note]').text()).toContain('Showing 6 of 8 series')
  })

  it('picking groups 0 and 6 gives distinct colour slots and the table follows the selection', async () => {
    const points = [
      ...Array.from({ length: 8 }, (_, i) => pt('2024', String(i), { k: i })),
      ...Array.from({ length: 8 }, (_, i) => pt('2023', String(i + 10), { k: i })),
    ]
    const { w } = await mountAt('/explorer/s/I/DE', (url) => (url === '/api/sources' ? sources : { points, history: [], attribution: null }))
    const sel = w.get('#ex-series').element as HTMLSelectElement
    ;[...sel.options].forEach((o, i) => { o.selected = i === 0 || i === 6 })
    await w.get('#ex-series').trigger('change')
    await flushPromises()
    const series = JSON.parse(w.get('[data-series]').attributes('data-series')!)
    expect(series.map((s: { colorIndex: number }) => s.colorIndex)).toEqual([0, 1])
    expect(series.map((s: { label: string }) => s.label)).toEqual(['k=0', 'k=6'])
    const rows = w.findAll('section[aria-labelledby=ex-points-h] tbody tr').map((r) => r.findAll('td').map((c) => c.text()))
    expect(rows).toEqual([['2023', 'k=0', '10'], ['2023', 'k=6', '16'], ['2024', 'k=0', '0'], ['2024', 'k=6', '6']])
  })

  it('a lone empty country skips the country step', async () => {
    const calls: string[] = []
    const { w } = await mountAt('/explorer/s/KPI', (url, params) => {
    calls.push(url + JSON.stringify(params ?? {}))
      if (url === '/api/sources') return sources
      if (url.endsWith('/countries')) return { items: [{ country: '', points: 3, last_changed: null }], total: 1 }
      return { points: [pt('2024-01', '5')], history: [], attribution: null }
    })
    expect(calls.some((c) => c.includes('/series') && c.includes('"country":""'))).toBe(true)
    expect(w.text()).toContain('Data points')
  })

  it('indicator step lists indicators and links on', async () => {
    const { w } = await mountAt('/explorer/s', (url) => (url === '/api/sources' ? sources : { items: [{ code: 'A', name: 'Alpha', unit: null, points: 1500 }], total: 1 }))
    expect(w.get('tbody a').attributes('href')).toBe('/explorer/s/A')
    expect(w.text()).toContain('1,500')
  })
})
