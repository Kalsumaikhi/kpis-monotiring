import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import type { KpiRow } from '../api/types'

const apiGet = vi.fn()
vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, apiGet: (...a: unknown[]) => apiGet(...a) }
})
import KpiFrequencyPage from '../pages/KpiFrequencyPage.vue'
import { fmtInterval } from '../lib/format'

const kpi = (over: Partial<KpiRow> = {}): KpiRow => ({
  source: 'worldbank', indicator: 'NY.GDP', name: 'GDP', unit: null, changes_30d: 1234, changes_12m: 5000, changes_all: 9000,
  first_seen: null, last_changed: '2026-11-20', typical_interval_days: 12, sparkline: [0, 1, 2, 0, 0, 3, 1, 0, 0, 2, 4, 1], ...over,
})

async function mountPage(path = '/kpis') {
  apiGet.mockImplementation(async (url: string) => (url === '/api/sources' ? [{ slug: 'worldbank', name: 'World Bank' }] : { items: [kpi()], total: 1 }))
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/kpis', name: 'kpis', component: KpiFrequencyPage },
      { path: '/explorer/:slug?/:indicator?/:country?', name: 'explorer', component: { template: '<div/>' } },
    ],
  })
  router.push(path)
  await router.isReady()
  const w = mount(KpiFrequencyPage, { global: { plugins: [router] } })
  await flushPromises()
  return { w, router }
}
beforeEach(() => { apiGet.mockReset() })

describe('KPI frequency', () => {
  it('interval formatting', () => {
    expect(fmtInterval(12)).toBe('≈ every 12 days')
    expect(fmtInterval(182)).toBe('≈ every 6 months')
    expect(fmtInterval(null)).toBe('—')
  })

  it('renders formatted row', async () => {
    const { w } = await mountPage()
    expect(w.text()).toContain('1,234')
    expect(w.text()).toContain('≈ every 12 days')
    expect(w.find('svg.spark').exists()).toBe(true)
  })

  it('kpi table sort changes query', async () => {
    const { w, router } = await mountPage()
    const btn = w.findAll('th button').find((b) => b.text().includes('Last changed'))!
    await btn.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query).toMatchObject({ sort: 'last_changed', order: 'desc' })
    expect(apiGet).toHaveBeenLastCalledWith('/api/kpis', expect.objectContaining({ sort: 'last_changed', order: 'desc' }))
    await btn.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query.order).toBe('asc')
  })

  it('row click opens explorer at the indicator', async () => {
    const { w, router } = await mountPage()
    await w.get('tbody tr').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/explorer/worldbank/NY.GDP')
  })

  it('search is debounced into the URL', async () => {
    vi.useFakeTimers()
    try {
      const { w, router } = await mountPage()
      await w.get('#k-q').setValue('gdp')
      vi.advanceTimersByTime(299)
      expect(router.currentRoute.value.query.q).toBeUndefined()
      vi.advanceTimersByTime(2)
      await flushPromises()
      expect(router.currentRoute.value.query.q).toBe('gdp')
    } finally {
      vi.useRealTimers()
    }
  })

  it('a pending search is cancelled when the route changes', async () => {
    vi.useFakeTimers()
    try {
      const { w, router } = await mountPage()
      await w.get('#k-q').setValue('stale')
      await router.push('/explorer/worldbank')
      vi.advanceTimersByTime(500)
      await flushPromises()
      expect(router.currentRoute.value.query.q).toBeUndefined()
    } finally {
      vi.useRealTimers()
    }
  })
})
