import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import type { Source } from '../api/types'

const apiGet = vi.fn()
const apiSend = vi.fn()
vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, apiGet: (...a: unknown[]) => apiGet(...a), apiSend: (...a: unknown[]) => apiSend(...a) }
})
import { ApiError } from '../api/client'
import OverviewPage from '../pages/OverviewPage.vue'
import { toasts, useToast } from '../composables/useToast'

function src(over: Partial<Source>): Source {
  return {
    slug: 's', name: 'S', area: 'Macro', access: 'api', enabled: true, cron: null,
    last_run: { id: 1, status: 'ok', finished_at: '2026-11-20T10:00:00Z', added: 0, altered: 0, removed: 0 },
    last_change_at: '2026-11-19T10:00:00Z', rows_tracked: 1234567, changes_30d: 5, failing_streak: 0,
    attribution: null, incomplete: false, noisy_targets: 0, logins_needed: 0, last_success_at: '2026-11-20T10:00:00Z',
    credential_id: null, credential_name: null, credential_supported: false, ...over,
  }
}

async function mountPage(list: Source[]) {
  apiGet.mockResolvedValue(list)
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: { template: '<div/>' } }] })
  const w = mount(OverviewPage, { global: { plugins: [router] } })
  await flushPromises()
  return w
}

const nonLast = { added: 0, altered: 0, removed: 0 }

beforeEach(() => { apiGet.mockReset(); apiSend.mockReset(); toasts.value = [] })

describe('OverviewPage', () => {
  it('sorts attention sources first', async () => {
    const w = await mountPage([
      src({ slug: 'a', name: 'Alpha' }),
      src({ slug: 'b', name: 'Beta', failing_streak: 2 }),
      src({ slug: 'c', name: 'Gamma', last_run: { id: 3, status: 'suspect_pending', finished_at: null, added: 0, altered: 0, removed: 0 } }),
    ])
    const names = w.findAll('[data-test=source-name]').map((n) => n.text())
    expect(names).toEqual(['Beta', 'Gamma', 'Alpha'])
    expect(w.get('[data-test=stat-attention]').text()).toContain('2')
    expect(w.get('[data-test=stat-tracked]').text()).toContain('3')
    expect(w.text()).toContain('1,234,567')
  })

  it('shows incomplete and failing flags with labels', async () => {
    const w = await mountPage([src({ incomplete: true, failing_streak: 3, noisy_targets: 2 })])
    expect(w.text()).toContain('Incomplete fetch')
    expect(w.text()).toContain('Failing 3× in a row')
    const noisy = w.get('a[href="/settings"]')
    expect(noisy.text()).toContain('2 noisy pages')
  })

  it('run now posts and shows toast', async () => {
    apiSend.mockResolvedValue({ run_id: 9 })
    const w = await mountPage([src({ slug: 'wb' })])
    await w.get('button.run-now').trigger('click')
    await flushPromises()
    expect(apiSend).toHaveBeenCalledWith('POST', '/api/sources/wb/run')
    expect(toasts.value.map((t) => t.message)).toEqual(['Check queued'])
  })

  it('409 shows Already running', async () => {
    apiSend.mockRejectedValue(new ApiError(409, 'source already has a queued or running run'))
    const w = await mountPage([src({ slug: 'wb' })])
    await w.get('button.run-now').trigger('click')
    await flushPromises()
    expect(toasts.value.map((t) => t.message)).toEqual(['Already running'])
  })

  it('other errors show the detail', async () => {
    apiSend.mockRejectedValue(new ApiError(500, 'boom'))
    const w = await mountPage([src({})])
    await w.get('button.run-now').trigger('click')
    await flushPromises()
    expect(toasts.value[0].message).toBe("Couldn't start check: boom")
  })

  it('shows empty state with settings link', async () => {
    const w = await mountPage([])
    expect(w.text()).toContain('No sources yet')
    expect(w.find('a[href="/settings"]').exists()).toBe(true)
  })

  it('running latest run shows last success time and In progress, never "never"', async () => {
    const past = new Date(Date.now() - 2 * 86400e3).toISOString()
    const w = await mountPage([src({ last_success_at: past, last_run: { id: 5, status: 'running', finished_at: null, ...nonLast } })])
    const t = w.text()
    expect(t).toContain('Last success')
    expect(t).toContain('2 days ago')
    expect(t).toContain('In progress')
    expect(t).not.toContain('never')
  })

  it('shows never for last success only when null', async () => {
    const w = await mountPage([src({ last_success_at: null })])
    expect(w.text()).toContain('never')
  })

  it('shows a loading state', async () => {
    apiGet.mockReturnValue(new Promise(() => {}))
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: { template: '<div/>' } }] })
    const w = mount(OverviewPage, { global: { plugins: [router] } })
    await flushPromises()
    expect(w.text()).toContain('Loading sources')
  })

  it('shows error with Retry that reloads', async () => {
    apiGet.mockRejectedValueOnce(new ApiError(500, 'db down'))
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: { template: '<div/>' } }] })
    const w = mount(OverviewPage, { global: { plugins: [router] } })
    await flushPromises()
    expect(w.get('[role=alert]').text()).toBe('db down')
    apiGet.mockResolvedValue([src({ name: 'Back' })])
    await w.get('button.btn.secondary').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('Back')
  })

  it('error toasts persist, info toasts auto-dismiss', () => {
    vi.useFakeTimers()
    const { show } = useToast()
    try {
      show('ok'); show('bad', 'error')
      vi.advanceTimersByTime(6000)
      expect(toasts.value.map((t) => t.message)).toEqual(['bad'])
    } finally {
      vi.useRealTimers()
    }
  })
})
