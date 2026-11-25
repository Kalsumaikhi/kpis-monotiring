import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'

const apiGet = vi.fn()
const apiSend = vi.fn()
vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, apiGet: (...a: unknown[]) => apiGet(...a), apiSend: (...a: unknown[]) => apiSend(...a) }
})
import { ApiError } from '../api/client'
import ChangeFeedPage from '../pages/ChangeFeedPage.vue'
import RunDetailPage from '../pages/RunDetailPage.vue'
import { toasts } from '../composables/useToast'

function mkRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/changes', name: 'changes', component: ChangeFeedPage },
      { path: '/runs/:id', name: 'run', component: RunDetailPage },
      { path: '/:p(.*)*', component: { template: '<div/>' } },
    ],
  })
}

const run = (over: Record<string, unknown> = {}) => ({
  id: 7, source: 'wb', status: 'suspect_pending', trigger: 'schedule', started_at: '2026-11-20T10:00:00Z',
  finished_at: '2026-11-20T10:05:00Z', vintage: '2026-11', rows_total: 900, added: 0, altered: 0, removed: 100,
  error: null, notify_error: null, top_indicators: [{ indicator: 'GDP', count: 100 }], ...over,
})
const ch = (over: Record<string, unknown> = {}) => ({
  change_type: 'removed', dataset: 'd', indicator: 'GDP', country: 'AT', period: '2020', dims: null, old: '1.5', new: null, ...over,
})

beforeEach(() => { apiGet.mockReset(); apiSend.mockReset(); toasts.value = [] })
afterEach(() => { vi.useRealTimers() })

async function mountDetail(handlers: (path: string, params?: Record<string, unknown>) => unknown) {
  apiGet.mockImplementation(async (path: string, params?: Record<string, unknown>) => handlers(path, params))
  const router = mkRouter()
  await router.push('/runs/7')
  const w = mount(RunDetailPage, { global: { plugins: [router] }, attachTo: document.body })
  await flushPromises()
  return w
}

describe('ChangeFeedPage', () => {
  it('feed filters sync to url', async () => {
    apiGet.mockImplementation(async (path: string) =>
      path === '/api/sources' ? [{ slug: 'wb', name: 'World Bank' }] : { items: [], total: 0 })
    const router = mkRouter()
    await router.push('/changes')
    const w = mount(ChangeFeedPage, { global: { plugins: [router] } })
    await flushPromises()
    const first = apiGet.mock.calls.find((c) => c[0] === '/api/runs')!
    expect(first[1]).toMatchObject({ has_changes: true, page: 1 })

    await w.get('#f-source').setValue('wb')
    await flushPromises()
    expect(router.currentRoute.value.query.source).toBe('wb')
    const last = apiGet.mock.calls.filter((c) => c[0] === '/api/runs').at(-1)!
    expect(last[1]).toMatchObject({ source: 'wb' })

    await w.get('input[type=checkbox]').setValue(false)
    await flushPromises()
    expect(router.currentRoute.value.query.changes).toBe('0')
    expect(apiGet.mock.calls.filter((c) => c[0] === '/api/runs').at(-1)![1].has_changes).toBeUndefined()
  })
})

describe('RunDetailPage', () => {
  it('renders pending banner and approves', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
    let status = 'suspect_pending'
    const w = await mountDetail((path) => {
      if (path === '/api/runs/7') return run({ status })
      if (path === '/api/runs/7/changes') return { items: [ch()], total: 1, pending: true }
      return []
    })
    expect(w.text()).toContain('held for review — 100 of 1,000 rows would be removed')
    apiSend.mockResolvedValue({ run_id: 7, status: 'approve_queued' })
    const approve = w.findAll('button').find((b) => b.text() === 'Approve')!
    await approve.trigger('click')
    expect(apiSend).not.toHaveBeenCalled() // confirm first
    const dlg = document.body.querySelector('[role=dialog]')!
    ;(Array.from(dlg.querySelectorAll('button')).find((b) => b.textContent === 'Approve') as HTMLButtonElement).click()
    await flushPromises()
    expect(apiSend).toHaveBeenCalledWith('POST', '/api/runs/7/approve')
    expect(w.text()).toContain('Applying…')
    status = 'ok'
    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()
    expect(w.text()).not.toContain('held for review')
    expect(w.text()).not.toContain('Applying…')
    w.unmount()
  })

  it('409 on approve shows the API detail', async () => {
    const w = await mountDetail((path) => (path === '/api/runs/7' ? run() : path.endsWith('/changes') ? { items: [], total: 0, pending: true } : []))
    apiSend.mockRejectedValue(new ApiError(409, 'run 7 is not the latest suspect_pending run'))
    await w.findAll('button').find((b) => b.text() === 'Approve')!.trigger('click')
    ;(Array.from(document.body.querySelectorAll('[role=dialog] button')).find((b) => b.textContent === 'Approve') as HTMLButtonElement).click()
    await flushPromises()
    expect(toasts.value.at(-1)).toMatchObject({ message: 'run 7 is not the latest suspect_pending run', tone: 'error' })
    w.unmount()
  })

  it('page change rows show excerpt lines', async () => {
    const w = await mountDetail((path) => {
      if (path === '/api/runs/7') return run({ status: 'ok' })
      if (path.endsWith('/changes')) return { items: [ch({ dataset: 'page', change_type: 'altered', old: 'abc', new: 'def', excerpt: { added: ['new <b>text</b>'], removed: ['old text'] } })], total: 1 }
      return []
    })
    const ex = w.get('[data-test=excerpt]')
    expect(ex.text()).toContain('+ new <b>text</b>')
    expect(ex.text()).toContain('− old text')
    expect(ex.find('b').exists()).toBe(false) // escaped, never v-html
    expect(w.text()).not.toContain('abc')
    w.unmount()
  })

  it('reject asks for confirmation', async () => {
    const w = await mountDetail((path) => (path === '/api/runs/7' ? run() : path.endsWith('/changes') ? { items: [], total: 0, pending: true } : []))
    apiSend.mockResolvedValue({ id: 7, status: 'rejected' })
    await w.findAll('button').find((b) => b.text() === 'Reject')!.trigger('click')
    expect(apiSend).not.toHaveBeenCalled()
    const dlg = document.body.querySelector('[role=dialog]')!
    expect(dlg.textContent).toContain('Reject these changes?')
    ;(Array.from(dlg.querySelectorAll('button')).find((b) => b.textContent === 'Cancel') as HTMLButtonElement).click()
    await flushPromises()
    expect(apiSend).not.toHaveBeenCalled()
    await w.findAll('button').find((b) => b.text() === 'Reject')!.trigger('click')
    ;(Array.from(document.body.querySelectorAll('[role=dialog] button')).find((b) => b.textContent === 'Reject') as HTMLButtonElement).click()
    await flushPromises()
    expect(apiSend).toHaveBeenCalledWith('POST', '/api/runs/7/reject')
    w.unmount()
  })

  it('404 shows Run not found', async () => {
    apiGet.mockRejectedValue(new ApiError(404, 'run not found'))
    const router = mkRouter()
    await router.push('/runs/7')
    const w = mount(RunDetailPage, { global: { plugins: [router] } })
    await flushPromises()
    expect(w.text()).toContain('Run not found')
  })

  it('stops polling after 10 minutes and on unmount', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
    const w = await mountDetail((path) => (path === '/api/runs/7' ? run({ status: 'approve_queued' }) : path.endsWith('/changes') ? { items: [], total: 0, pending: true } : []))
    await vi.advanceTimersByTimeAsync(10 * 60 * 1000 + 5000)
    expect(w.text()).toContain('Still applying — refresh later')
    const n = apiGet.mock.calls.length
    await vi.advanceTimersByTimeAsync(20000)
    expect(apiGet.mock.calls.length).toBe(n)
    w.unmount()
  })
})
