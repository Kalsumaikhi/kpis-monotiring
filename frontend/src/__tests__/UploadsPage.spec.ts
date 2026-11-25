import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'

const apiGet = vi.fn()
const apiUpload = vi.fn()
vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, apiGet: (...a: unknown[]) => apiGet(...a), apiUpload: (...a: unknown[]) => apiUpload(...a) }
})
import { ApiError } from '../api/client'
import UploadsPage from '../pages/UploadsPage.vue'

const manual = [{ slug: 'tge', name: 'TheGlobalEconomy.com', access: 'manual' }]
const upl = { id: 1, source: 'tge', who: 'Ana', filename: 'old.xlsx', created_at: '2026-11-20T10:00:00Z', run_id: 7, run_status: 'ok' }

async function mountPage(sources: unknown[], uploads: unknown[] = []) {
  apiGet.mockImplementation(async (path: string) => (path === '/api/sources' ? sources : { items: uploads, total: uploads.length }))
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: { template: '<div/>' } }] })
  const w = mount(UploadsPage, { global: { plugins: [router] } })
  await flushPromises()
  return w
}

beforeEach(() => { apiGet.mockReset(); apiUpload.mockReset(); try { localStorage.clear() } catch { /* ignore */ } })

describe('UploadsPage', () => {
  it('empty state without manual sources', async () => {
    const w = await mountPage([])
    expect(w.text()).toContain('No upload sources configured yet. Sources like TheGlobalEconomy.com are added once a sample export is available.')
    expect(w.text()).toContain('Want to watch web pages or API links instead? Add them in Settings.')
    expect(w.find('form').exists()).toBe(false)
  })

  it('upload posts form and links to run', async () => {
    apiUpload.mockResolvedValue({ run_id: 42 })
    const w = await mountPage(manual, [upl])
    expect(w.text()).toContain('old.xlsx')
    await w.get('#up-who').setValue('Bo')
    const input = w.get('#up-file')
    const file = new File(['abc'], 'new.xlsx')
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
    await input.trigger('change')
    await w.get('form').trigger('submit')
    await flushPromises()
    const [path, fd] = apiUpload.mock.calls[0] as [string, FormData]
    expect(path).toBe('/api/uploads')
    expect(fd.get('source')).toBe('tge')
    expect(fd.get('who')).toBe('Bo')
    expect((fd.get('file') as File).name).toBe('new.xlsx')
    const link = w.findAll('a').find((a) => a.text() === 'Review changes')!
    expect(link.attributes('href')).toBe('/runs/42')
    expect(apiGet.mock.calls.filter((c) => c[0] === '/api/uploads').length).toBe(2)
    expect(localStorage.getItem('kpi.uploader')).toBe('Bo')
  })

  it('refuses files over 50 MB', async () => {
    const w = await mountPage(manual)
    await w.get('#up-who').setValue('Bo')
    const big = new File(['x'], 'big.xlsx')
    Object.defineProperty(big, 'size', { value: 51 * 1024 * 1024 })
    const input = w.get('#up-file')
    Object.defineProperty(input.element, 'files', { value: [big], configurable: true })
    await input.trigger('change')
    expect(w.text()).toContain('File is larger than 50 MB')
    expect(w.get('button[type=submit]').attributes('disabled')).toBeDefined()
  })

  async function fill(w: Awaited<ReturnType<typeof mountPage>>) {
    await w.get('#up-who').setValue('  Bo  ')
    const input = w.get('#up-file')
    Object.defineProperty(input.element, 'files', { value: [new File(['a'], 'n.xlsx')], configurable: true })
    await input.trigger('change')
  }

  it.each([[413, 'file too large (max 50 MB)', 'File is larger than 50 MB'], [404, 'source not found', 'source not found'], [422, 'uploads are only accepted for manual sources', 'uploads are only accepted for manual sources']])(
    'shows error for %i', async (status, detail, shown) => {
      apiUpload.mockRejectedValue(new ApiError(status, detail))
      const w = await mountPage(manual)
      await fill(w)
      await w.get('form').trigger('submit')
      await flushPromises()
      expect(w.get('[role=alert]').text()).toBe(shown)
      expect(w.text()).not.toContain('Review changes')
    })

  it('disables Upload while in flight and trims name', async () => {
    let resolve!: (v: unknown) => void
    apiUpload.mockReturnValue(new Promise((r) => { resolve = r }))
    const w = await mountPage(manual)
    expect(w.text()).toContain('Choose a source, enter your name and pick a file')
    await fill(w)
    await w.get('form').trigger('submit')
    await flushPromises()
    expect(w.get('button[type=submit]').attributes('disabled')).toBeDefined()
    expect(w.get('button[type=submit]').text()).toBe('Upload')
    expect(w.get('[role=status]').text()).toContain('Uploading…')
    expect((apiUpload.mock.calls[0][1] as FormData).get('who')).toBe('Bo')
    resolve({ run_id: 5 })
    await flushPromises()
    expect(w.get('button[type=submit]').attributes('disabled')).toBeDefined()
  })
})
