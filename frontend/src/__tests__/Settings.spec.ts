import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const apiGet = vi.fn()
const apiSend = vi.fn()
const apiUpload = vi.fn()
vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return {
    ...actual,
    apiGet: (...a: unknown[]) => apiGet(...a),
    apiSend: (...a: unknown[]) => apiSend(...a),
    apiUpload: (...a: unknown[]) => apiUpload(...a),
  }
})
import { ApiError } from '../api/client'
import TargetForm from '../pages/settings/TargetForm.vue'
import ImportDialog from '../pages/settings/ImportDialog.vue'
import CustomSources from '../pages/settings/CustomSources.vue'
import BuiltinSources from '../pages/settings/BuiltinSources.vue'
import { toasts } from '../composables/useToast'

beforeEach(() => {
  apiGet.mockReset(); apiSend.mockReset(); apiUpload.mockReset(); toasts.value = []
  document.body.innerHTML = ''
})

const target = (over: Record<string, unknown> = {}) => ({
  id: 1, kpi_name: 'Pricing page', url: 'https://example.com/pricing', kind: 'auto', selector: null,
  use_browser: false, enabled: true, last_status: 'ok', last_checked_at: '2026-11-20T10:00:00Z', last_error: null, noisy: false, ...over,
})
const custom = (targets = [target()]) => [{ slug: 'custom-acme', name: 'Acme', enabled: true, cron: '0 3 * * *', targets }]

describe('TargetForm', () => {
  it('form disables save until valid', async () => {
    const w = mount(TargetForm, { props: { sourceNames: ['Acme'] } })
    const save = () => w.get('button[type=submit]')
    expect(save().attributes('disabled')).toBeDefined()
    await w.get('#tf-source').setValue('Acme')
    await w.get('#tf-kpi').setValue('Pricing')
    expect(save().attributes('disabled')).toBeDefined()
    await w.get('#tf-url').setValue('ftp://nope')
    expect(save().attributes('disabled')).toBeDefined()
    await w.get('#tf-url').setValue('https://example.com/p')
    expect(save().attributes('disabled')).toBeUndefined()
  })

  it('test link shows text preview', async () => {
    apiSend.mockResolvedValue({ status: 'ok', kind: 'page', text_preview: 'Plans <b>from</b> 10 EUR' })
    const w = mount(TargetForm, { props: { sourceNames: [] } })
    await w.get('#tf-url').setValue('https://example.com/p')
    await w.get('[data-test=test-link]').trigger('click')
    await flushPromises()
    expect(apiSend).toHaveBeenCalledWith('POST', '/api/watch-targets/test', expect.objectContaining({ url: 'https://example.com/p' }))
    const pre = w.get('pre')
    expect(pre.text()).toBe('Plans <b>from</b> 10 EUR')
    expect(pre.find('b').exists()).toBe(false)
  })

  it('test link explains a blocked destination', async () => {
    apiSend.mockResolvedValue({ status: 'blocked', kind: 'page', error: 'destination not allowed' })
    const w = mount(TargetForm, { props: { sourceNames: [] } })
    await w.get('#tf-url').setValue('http://10.0.0.1/')
    await w.get('[data-test=test-link]').trigger('click')
    await flushPromises()
    expect(w.text()).toContain("Internal addresses aren't allowed")
  })

  it('shows 409 inline on save', async () => {
    apiSend.mockRejectedValue(new ApiError(409, 'a KPI with this link already exists'))
    const w = mount(TargetForm, { props: { sourceNames: [] } })
    await w.get('#tf-source').setValue('Acme')
    await w.get('#tf-kpi').setValue('Pricing')
    await w.get('#tf-url').setValue('https://example.com/p')
    await w.get('form').trigger('submit')
    await flushPromises()
    expect(w.get('[role=alert]').text()).toContain('already exists')
  })
})

describe('ImportDialog', () => {
  const rows = [
    { row: 2, source_name: 'Acme', kpi_name: 'A', url: 'https://a.example', status: 'new_source' },
    { row: 3, source_name: 'Acme', kpi_name: 'B', url: 'https://b.example', status: 'duplicate' },
    { row: 4, source_name: 'Acme', kpi_name: 'C', url: 'ftp://c', status: 'invalid', error: 'link must start with http(s)' },
  ]
  it('import preview shows statuses and imports valid count', async () => {
    apiUpload.mockResolvedValue({ token: 'tok123', rows })
    apiSend.mockResolvedValue({ created_sources: 1, created_targets: 1, skipped: 1 })
    const w = mount(ImportDialog, { props: { open: true }, attachTo: document.body })
    const input = document.body.querySelector('input[type=file]') as HTMLInputElement
    Object.defineProperty(input, 'files', { value: [new File(['x'], 'k.csv', { type: 'text/csv' })] })
    input.dispatchEvent(new Event('change'))
    await flushPromises()
    const body = document.body.textContent ?? ''
    expect(body).toContain('New source')
    expect(body).toContain('skipped')
    expect(body).toContain('link must start with http(s)')
    const btn = Array.from(document.body.querySelectorAll('button')).find((b) => /^Import \d+ KPIs$/.test(b.textContent!.trim()))!
    expect(btn.textContent!.trim()).toBe('Import 1 KPIs')
    btn.click()
    await flushPromises()
    expect(apiSend).toHaveBeenCalledWith('POST', '/api/watch-targets/import/confirm', { token: 'tok123' })
    expect(w.emitted('imported')).toBeTruthy()
    w.unmount()
  })

  it('shows expired message on 410', async () => {
    apiUpload.mockResolvedValue({ token: 't', rows: [rows[0]] })
    apiSend.mockRejectedValue(new ApiError(410, 'import preview expired'))
    const w = mount(ImportDialog, { props: { open: true }, attachTo: document.body })
    const input = document.body.querySelector('input[type=file]') as HTMLInputElement
    Object.defineProperty(input, 'files', { value: [new File(['x'], 'k.csv')] })
    input.dispatchEvent(new Event('change'))
    await flushPromises()
    ;(Array.from(document.body.querySelectorAll('button')).find((b) => /^Import 1 KPIs$/.test(b.textContent!.trim())) as HTMLElement).click()
    await flushPromises()
    expect(document.body.textContent).toContain('Preview expired — upload again')
    w.unmount()
  })

  it('template download has header', async () => {
    let blob: Blob | null = null
    const create = vi.fn((b: Blob) => { blob = b; return 'blob:fake' })
    ;(URL as unknown as Record<string, unknown>).createObjectURL = create
    ;(URL as unknown as Record<string, unknown>).revokeObjectURL = vi.fn()
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const w = mount(ImportDialog, { props: { open: true }, attachTo: document.body })
    ;(document.body.querySelector('[data-test=template]') as HTMLElement).click()
    expect(create).toHaveBeenCalled()
    expect(click).toHaveBeenCalled()
    const text = await new Promise<string>((res) => {
      const r = new FileReader()
      r.onload = () => res(String(r.result))
      r.readAsText(blob!)
    })
    expect(text.split(/\r?\n/)[0]).toBe('Source name,KPI name,Link,Key name')
    click.mockRestore()
    w.unmount()
  })
})

describe('CustomSources', () => {
  it('delete asks confirmation with no-alert text', async () => {
    apiGet.mockResolvedValue(custom())
    apiSend.mockResolvedValue(null)
    const w = mount(CustomSources, { attachTo: document.body })
    await flushPromises()
    await w.get('[data-test=delete-1]').trigger('click')
    expect(document.body.textContent).toContain("Deleting removes this KPI's history. No alert is sent.")
    expect(apiSend).not.toHaveBeenCalled()
    const confirm = Array.from(document.body.querySelectorAll('.dialog button')).find((b) => b.textContent!.trim() === 'Delete') as HTMLElement
    confirm.click()
    await flushPromises()
    expect(apiSend).toHaveBeenCalledWith('DELETE', '/api/watch-targets/1')
    w.unmount()
  })

  it('noisy chip opens edit form with advanced open', async () => {
    apiGet.mockResolvedValue(custom([target({ noisy: true })]))
    const w = mount(CustomSources, { attachTo: document.body })
    await flushPromises()
    await w.get('[data-test=noisy-1]').trigger('click')
    await flushPromises()
    expect(w.find('#tf-selector').isVisible()).toBe(true)
    w.unmount()
  })

  it('disable patches enabled false and shows 409 detail', async () => {
    apiGet.mockResolvedValue(custom())
    apiSend.mockRejectedValue(new ApiError(409, 'source is running, try again'))
    const w = mount(CustomSources, { attachTo: document.body })
    await flushPromises()
    await w.get('[data-test=toggle-1]').trigger('click')
    await flushPromises()
    expect(apiSend).toHaveBeenCalledWith('PATCH', '/api/watch-targets/1', { enabled: false })
    expect(toasts.value.some((t) => t.message.includes('source is running'))).toBe(true)
    w.unmount()
  })
})

describe('BuiltinSources', () => {
  const src = (slug: string, access: string) => ({ slug, name: slug.toUpperCase(), area: 'x', access, enabled: true, cron: '0 3 * * 1', last_run: null })
  it('lists only non-watch sources and shows Invalid schedule on 422', async () => {
    apiGet.mockResolvedValue([src('wb', 'api'), src('custom-acme', 'watch')])
    apiSend.mockRejectedValue(new ApiError(422, 'invalid cron expression'))
    const w = mount(BuiltinSources)
    await flushPromises()
    expect(w.text()).toContain('WB')
    expect(w.text()).not.toContain('CUSTOM-ACME')
    await w.get('[data-test=cron-wb]').setValue('bad')
    await w.get('[data-test=save-wb]').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('Invalid schedule')
  })
  it('run now posts', async () => {
    apiGet.mockResolvedValue([src('wb', 'api')])
    apiSend.mockResolvedValue({ run_id: 5 })
    const w = mount(BuiltinSources)
    await flushPromises()
    await w.get('[data-test=run-wb]').trigger('click')
    await flushPromises()
    expect(apiSend).toHaveBeenCalledWith('POST', '/api/sources/wb/run')
  })
  it('Enter in cron field is guarded: unchanged sends nothing, busy sends once', async () => {
    apiGet.mockResolvedValue([src('wb', 'api')])
    let release: (v: unknown) => void = () => {}
    apiSend.mockImplementation(() => new Promise((r) => { release = r }))
    const w = mount(BuiltinSources)
    await flushPromises()
    const input = w.get('[data-test=cron-wb]')
    await input.trigger('keydown', { key: 'Enter' })
    expect(apiSend).not.toHaveBeenCalled()
    await input.setValue('  0 4 * * *  ')
    await input.trigger('keydown', { key: 'Enter' })
    await input.trigger('keydown', { key: 'Enter' })
    expect(apiSend).toHaveBeenCalledTimes(1)
    expect(apiSend).toHaveBeenCalledWith('PATCH', '/api/sources/wb', { cron: '0 4 * * *' })
    release({ slug: 'wb', enabled: true, cron: '0 4 * * *' })
  })
})

describe('TargetForm extras', () => {
  it('clears the test preview when the link changes', async () => {
    apiSend.mockResolvedValue({ status: 'ok', kind: 'page', text_preview: 'hi' })
    const w = mount(TargetForm, { props: { sourceNames: [] } })
    await w.get('#tf-url').setValue('https://example.com/p')
    await w.get('[data-test=test-link]').trigger('click')
    await flushPromises()
    expect(w.find('pre').exists()).toBe(true)
    await w.get('#tf-url').setValue('https://example.com/q')
    expect(w.find('pre').exists()).toBe(false)
  })
  it('robots block uses its own wording', async () => {
    apiSend.mockResolvedValue({ status: 'blocked', kind: 'page', error: 'blocked by robots.txt' })
    const w = mount(TargetForm, { props: { sourceNames: [] } })
    await w.get('#tf-url').setValue('https://example.com/p')
    await w.get('[data-test=test-link]').trigger('click')
    await flushPromises()
    expect(w.text()).toContain("This site doesn't allow automated access")
    expect(w.text()).not.toContain("Internal addresses")
  })
})
