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
import KeyForm from '../pages/settings/KeyForm.vue'
import KeysCard from '../pages/settings/KeysCard.vue'
import TargetForm from '../pages/settings/TargetForm.vue'
import ImportDialog from '../pages/settings/ImportDialog.vue'
import BuiltinSources from '../pages/settings/BuiltinSources.vue'
import { toasts } from '../composables/useToast'

const key = (over: Record<string, unknown> = {}) => ({
  id: 1, name: 'Acme token', kind: 'header', header_name: 'Authorization', prefix: 'Bearer', param_name: null, username: null,
  has_secret: true, readable: true, created_at: '2026-11-01T10:00:00Z', updated_at: '2026-11-01T10:00:00Z',
  last_used_at: null, last_error: null, used_by: { targets: [], sources: [] }, ...over,
})

beforeEach(() => {
  apiGet.mockReset(); apiSend.mockReset(); apiUpload.mockReset(); toasts.value = []
  document.body.innerHTML = ''
})

async function card(keys = [key()]) {
  apiGet.mockResolvedValue(keys)
  const w = mount(KeysCard, { attachTo: document.body })
  await flushPromises()
  return w
}

describe('KeysCard', () => {
  it('shows summaries and states, never a secret', async () => {
    const w = await card([
      key(),
      key({ id: 2, name: 'Query', kind: 'query', header_name: null, prefix: null, param_name: 'api_key', readable: false }),
      key({ id: 3, name: 'Login', kind: 'basic', header_name: null, prefix: null, username: 'alice', last_error: 'key rejected (HTTP 401)' }),
    ])
    const t = w.text()
    expect(t).toContain('Header Authorization: Bearer ••••')
    expect(t).toContain('Link parameter api_key=••••')
    expect(t).toContain('Username alice + password')
    expect(t).toContain('Set')
    expect(w.findAll('[data-test=needs-reentry]')).toHaveLength(1)
    expect(t).toContain('key rejected (HTTP 401)')
    expect(t).toContain('Used by 0')
    w.unmount()
  })

  it('never renders a secret anywhere in the card', async () => {
    const SENTINEL = 'sentinel-s3cr3t-value'
    const w = await card([key(), key({ id: 2, name: 'Q', kind: 'query', header_name: null, prefix: null, param_name: 'k' })])
    expect(w.html()).not.toContain(SENTINEL)
    expect(w.html()).not.toMatch(/secret"\s*:/)
    expect(w.text()).not.toContain(SENTINEL)
    expect(w.findAll('input[type=password]')).toHaveLength(0) // no secret input until a form is opened
    w.unmount()
  })

  it('secret input is empty after save, and again when the form is re-opened', async () => {
    const w = await card([])
    apiSend.mockResolvedValue({})
    await w.get('[data-test=add-key]').trigger('click')
    await w.get('#kf-name').setValue('New')
    await w.get('#kf-secret').setValue('sentinel-s3cr3t-value')
    const first = w.get('#kf-secret').element as HTMLInputElement
    await w.get('form').trigger('submit')
    await flushPromises()
    expect(first.value).toBe('')
    expect(w.html()).not.toContain('sentinel-s3cr3t-value')
    await w.get('[data-test=add-key]').trigger('click')
    expect((w.get('#kf-secret').element as HTMLInputElement).value).toBe('')
    w.unmount()
  })

  it('clears the secret after save and sends it only in the body', async () => {
    const w = await card([])
    apiSend.mockResolvedValue({})
    await w.get('[data-test=add-key]').trigger('click')
    await w.get('#kf-name').setValue('New')
    const input = w.get('#kf-secret')
    expect(input.attributes('type')).toBe('password')
    expect(input.attributes('autocomplete')).toBe('new-password')
    await input.setValue('s3cr3t')
    await w.get('[data-test=toggle-secret]').trigger('click')
    expect(w.get('#kf-secret').attributes('type')).toBe('text')
    await w.get('form').trigger('submit')
    await flushPromises()
    expect(apiSend).toHaveBeenCalledWith('POST', '/api/credentials', expect.objectContaining({ name: 'New', kind: 'header', secret: 's3cr3t' }))
    expect(apiSend.mock.calls[0][1]).not.toContain('s3cr3t')
    w.unmount()
  })

  it('form input is empty after a successful save', async () => {
    apiSend.mockResolvedValue({})
    const w = mount(KeyForm, { attachTo: document.body })
    await w.get('#kf-name').setValue('N')
    await w.get('#kf-secret').setValue('s3cr3t')
    await w.get('form').trigger('submit')
    await flushPromises()
    expect(w.emitted('saved')).toBeTruthy()
    expect((w.get('#kf-secret').element as HTMLInputElement).value).toBe('')
    w.unmount()
  })

  it('clears the secret after a failed save too', async () => {
    const w = await card([])
    apiSend.mockRejectedValue(new ApiError(409, 'a key with this name already exists'))
    await w.get('[data-test=add-key]').trigger('click')
    await w.get('#kf-name').setValue('Dup')
    await w.get('#kf-secret').setValue('s3cr3t')
    await w.get('form').trigger('submit')
    await flushPromises()
    expect((w.get('#kf-secret').element as HTMLInputElement).value).toBe('')
    expect(w.text()).toContain('a key with this name already exists')
    w.unmount()
  })

  it('edit with blank secret omits secret field', async () => {
    const w = await card()
    apiSend.mockResolvedValue({})
    await w.get('[data-test=edit-1]').trigger('click')
    expect(w.text()).toContain('Leave blank to keep the current secret')
    expect((w.get('#kf-secret').element as HTMLInputElement).value).toBe('')
    await w.get('#kf-name').setValue('Renamed')
    await w.get('form').trigger('submit')
    await flushPromises()
    const [method, path, body] = apiSend.mock.calls[0]
    expect(method).toBe('PATCH')
    expect(path).toBe('/api/credentials/1')
    expect(body).toMatchObject({ name: 'Renamed', kind: 'header' })
    expect('secret' in (body as object)).toBe(false)
    w.unmount()
  })

  it('replace secret sends only the secret', async () => {
    const w = await card([key({ readable: false })])
    apiSend.mockResolvedValue({})
    await w.get('[data-test=replace-1]').trigger('click')
    await w.get('#kf-secret').setValue('new-one')
    await w.get('form').trigger('submit')
    await flushPromises()
    expect(apiSend).toHaveBeenCalledWith('PATCH', '/api/credentials/1', { secret: 'new-one' })
    w.unmount()
  })

  it('delete 409 lists users', async () => {
    const used = { targets: [{ id: 5, kpi_name: 'Pricing page', source: 'Acme' }], sources: ['fred'] }
    const w = await card([key({ used_by: used })])
    apiSend.mockRejectedValue(new ApiError(409, 'key is in use', { detail: 'key is in use', used_by: used }))
    await w.get('[data-test=delete-1]').trigger('click')
    await flushPromises()
    ;(document.body.querySelector('.dialog .btn.danger') as HTMLElement).click()
    await flushPromises()
    const panel = w.get('[data-test=in-use]').text()
    expect(panel).toContain('Pricing page')
    expect(panel).toContain('Acme')
    expect(panel).toContain('fred')
    expect(w.text()).toContain('Used by 2')
    w.unmount()
  })

  it('cancel clears the typed secret', async () => {
    const w = mount(KeyForm, { attachTo: document.body })
    const el = w.get('#kf-secret').element as HTMLInputElement
    await w.get('#kf-secret').setValue('abc')
    await w.get('[data-test=cancel]').trigger('click')
    expect(el.value).toBe('')
    expect(w.emitted('cancel')).toBeTruthy()
    w.unmount()
  })

  it('status configured=false shows the setup panel up front and disables writes', async () => {
    apiGet.mockImplementation((p: string) => Promise.resolve(p === '/api/credentials/status' ? { configured: false } : [key()]))
    const w = mount(KeysCard, { attachTo: document.body })
    await flushPromises()
    expect(w.find('[data-test=setup-panel]').exists()).toBe(true)
    expect(w.get('[data-test=add-key]').attributes('disabled')).toBeDefined()
    expect(w.get('[data-test=replace-1]').attributes('disabled')).toBeDefined()
    await w.get('[data-test=edit-1]').trigger('click')
    expect(w.get('#kf-secret').attributes('disabled')).toBeDefined()
    w.unmount()
  })

  it('503 shows setup panel and disables Add', async () => {
    const w = await card([])
    apiSend.mockRejectedValue(new ApiError(503, 'KPI_SECRET_KEY is not set — see README → Keys'))
    await w.get('[data-test=add-key]').trigger('click')
    await w.get('#kf-name').setValue('X')
    await w.get('#kf-secret').setValue('abc')
    await w.get('form').trigger('submit')
    await flushPromises()
    expect(w.get('[data-test=setup-panel]').text()).toContain("Keys are encrypted with KPI_SECRET_KEY, which isn't set. Ask whoever runs the server to set it (see README → Keys), then reload.")
    await w.get('[data-test=cancel]').trigger('click')
    expect(w.get('[data-test=add-key]').attributes('disabled')).toBeDefined()
    w.unmount()
  })
})

describe('TargetForm keys', () => {
  async function form() {
    apiGet.mockResolvedValue([key(), key({ id: 2, name: 'Other' })])
    const w = mount(TargetForm, { props: { sourceNames: [] } })
    await flushPromises()
    await w.get('#tf-source').setValue('Acme')
    await w.get('#tf-kpi').setValue('Pricing')
    return w
  }

  it('lists keys and sends credential_id on save and test', async () => {
    apiSend.mockResolvedValue({ status: 'ok', kind: 'page', text_preview: 'x' })
    const w = await form()
    expect(w.findAll('#tf-key option').map((o) => o.text())).toEqual(['None', 'Acme token', 'Other'])
    await w.get('#tf-url').setValue('https://example.com/p')
    await w.get('#tf-key').setValue(2)
    await w.get('[data-test=test-link]').trigger('click')
    await flushPromises()
    expect(apiSend).toHaveBeenCalledWith('POST', '/api/watch-targets/test', expect.objectContaining({ credential_id: 2 }))
    await w.get('form').trigger('submit')
    await flushPromises()
    expect(apiSend).toHaveBeenCalledWith('POST', '/api/custom-sources', expect.objectContaining({ credential_id: 2 }))
  })

  it('target form blocks key with http or browser', async () => {
    const w = await form()
    await w.get('#tf-url').setValue('http://example.com/p')
    expect(w.get('button[type=submit]').attributes('disabled')).toBeUndefined()
    await w.get('#tf-key').setValue(1)
    expect(w.get('[data-test=key-problem]').text()).toBe('keys can only be sent over https')
    expect(w.get('button[type=submit]').attributes('disabled')).toBeDefined()
    await w.get('#tf-url').setValue('https://example.com/p')
    expect(w.get('button[type=submit]').attributes('disabled')).toBeUndefined()
    await w.get('details input[type=checkbox]').setValue(true)
    expect(w.get('[data-test=key-problem]').text()).toBe('keys can\'t be used with "Needs a browser" yet')
    expect(w.get('button[type=submit]').attributes('disabled')).toBeDefined()
  })
})

describe('BuiltinSources key select', () => {
  it('shows a Key select only for supported sources and patches credential_id', async () => {
    const base = { area: 'x', access: 'api', enabled: true, cron: null, last_run: null, credential_id: null, credential_name: null }
    apiGet.mockImplementation((p: string) => Promise.resolve(p === '/api/credentials'
      ? [key()]
      : [{ ...base, slug: 'a', name: 'A', credential_supported: true, credential_id: 1 }, { ...base, slug: 'b', name: 'B', credential_supported: false }]))
    apiSend.mockResolvedValue({ slug: 'a', enabled: true, cron: null, credential_id: 1 })
    const w = mount(BuiltinSources)
    await flushPromises()
    expect(w.find('[data-test=key-a]').exists()).toBe(true)
    expect((w.get('[data-test=key-a]').element as HTMLSelectElement).value).toBe('1')
    expect(w.find('[data-test=key-b]').exists()).toBe(false)
    await w.get('[data-test=key-a]').setValue('')
    await flushPromises()
    expect(apiSend).toHaveBeenCalledWith('PATCH', '/api/sources/a', { credential_id: null })
  })
})

describe('ImportDialog keys', () => {
  it('template has Key name column and preview shows a Key column', async () => {
    let blob: Blob | null = null
    ;(URL as unknown as Record<string, unknown>).createObjectURL = vi.fn((b: Blob) => { blob = b; return 'blob:fake' })
    ;(URL as unknown as Record<string, unknown>).revokeObjectURL = vi.fn()
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    apiUpload.mockResolvedValue({ token: 't', rows: [
      { row: 2, source_name: 'A', kpi_name: 'K1', url: 'https://a.example/x', key_name: 'Acme token', status: 'new_source' },
      { row: 3, source_name: 'A', kpi_name: 'K2', url: 'https://a.example/y', status: 'invalid', error: 'unknown key: Nope', key_name: 'Nope' },
      { row: 4, source_name: 'A', kpi_name: 'K3', url: 'https://a.example/z', status: 'new_source' },
    ] })
    const w = mount(ImportDialog, { props: { open: true }, attachTo: document.body })
    ;(document.body.querySelector('[data-test=template]') as HTMLElement).click()
    const text = await new Promise<string>((res) => { const r = new FileReader(); r.onload = () => res(String(r.result)); r.readAsText(blob!) })
    expect(text.split(/\r?\n/)[0]).toBe('Source name,KPI name,Link,Key name')
    const input = document.body.querySelector('#imp-file') as HTMLInputElement
    Object.defineProperty(input, 'files', { value: [new File(['x'], 'a.csv')] })
    input.dispatchEvent(new Event('change'))
    await flushPromises()
    const cells = Array.from(document.body.querySelectorAll('[data-test=key-cell]')).map((c) => c.textContent)
    expect(cells).toEqual(['Acme token', 'Nope', '—'])
    expect(document.body.textContent).toContain('unknown key: Nope')
    click.mockRestore()
    w.unmount()
  })
})
