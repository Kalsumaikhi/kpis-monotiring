// Goes through the real api/client.ts (withParams, encoding); only fetch is stubbed.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'

vi.mock('../components/SeriesChart.vue', () => ({ default: { render: () => h('div') } }))
import ExplorerPage from '../pages/ExplorerPage.vue'

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

afterEach(() => vi.unstubAllGlobals())

describe('Explorer with a custom (watch) source', () => {
  it('encodes a KPI name with "/" and loads the country-less series', async () => {
    const calls: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      calls.push(url)
      const path = url.split('?')[0]
      if (path === '/api/sources') return json([{ slug: 'custom-x', name: 'X', area: 'custom', rows_tracked: 1 }])
      if (path === '/api/sources/custom-x/indicators/Price%20%2F%20unit/countries') {
        return json({ items: [{ country: '', points: 1, last_changed: null }], total: 1 })
      }
      if (path === '/api/sources/custom-x/series') {
        // Mirrors the backend: missing country means "" (watch sources)
        const country = new URL(url, 'http://x').searchParams.get('country') ?? ''
        if (country !== '') return json({ points: [], history: [], attribution: null })
        return json({ points: [{ period: 'current', dims: null, value: 'Sold out' }], history: [], attribution: null })
      }
      return json({ detail: 'Not Found' }, 404)
    }))
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/explorer/:slug?/:indicator?/:country?', name: 'explorer', component: ExplorerPage }],
    })
    await router.push({ name: 'explorer', params: { slug: 'custom-x', indicator: 'Price / unit' } })
    const w = mount(ExplorerPage, { global: { plugins: [router] } })
    await vi.waitFor(async () => {
      await flushPromises()
      expect(w.text()).toContain('Sold out')
    })
    expect(calls).toContain('/api/sources/custom-x/indicators/Price%20%2F%20unit/countries?page=1&page_size=50')
    expect(calls).toContain('/api/sources/custom-x/series?indicator=Price+%2F+unit')
    expect(w.text()).not.toContain('Not Found')
  })
})
