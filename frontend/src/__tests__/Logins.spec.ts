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
import KeyForm from '../pages/settings/KeyForm.vue'
import KeysCard from '../pages/settings/KeysCard.vue'
import TargetForm from '../pages/settings/TargetForm.vue'
import SignInDialog from '../components/SignInDialog.vue'
import StatusBadge from '../components/StatusBadge.vue'
import OverviewPage from '../pages/OverviewPage.vue'
import type { Source } from '../api/types'

const loginKey = (over: Record<string, unknown> = {}) => ({
  id: 7, name: 'Acme login', kind: 'login', header_name: null, prefix: null, param_name: null, username: 'alice',
  login_url: 'https://acme.test/login', selectors: null, extra_hosts: null, login_status: 'login_needed', login_error: null,
  has_secret: true, readable: true, created_at: null, updated_at: null, last_used_at: null, last_error: null,
  used_by: { targets: [], sources: [] }, ...over,
})

beforeEach(() => { apiGet.mockReset(); apiSend.mockReset(); document.body.innerHTML = '' })
afterEach(() => { vi.useRealTimers() })

describe('login key form', () => {
  it('login key form fields and validation', async () => {
    const w = mount(KeyForm, { attachTo: document.body })
    await w.get('input[value=login]').setValue(true)
    expect(w.find('#kf-loginurl').exists()).toBe(true)
    expect(w.text()).toContain("Logins always use the built-in browser. If a site emails you a code, you'll enter it here when signing in.")
    await w.get('#kf-name').setValue('Acme login')
    await w.get('#kf-loginurl').setValue('http://acme.test/login')
    await w.get('#kf-loginurl').trigger('blur')
    expect(w.text()).toContain('login page must be https')
    expect(w.get('#kf-loginurl').attributes('aria-invalid')).toBe('true')
    await w.get('#kf-loginurl').setValue('https://acme.test/login')
    await w.get('#kf-user').setValue('alice')
    await w.get('[data-test=secret]').setValue('hunter22pw')
    // Advanced: selectors and extra hosts
    await w.get('#kf-sel-username').setValue('#email')
    await w.get('#kf-sel-code').setValue('#otp')
    await w.get('#kf-hosts').setValue('app.acme.test, api.acme.test  cdn.acme.test')
    apiSend.mockResolvedValue({})
    await w.get('form').trigger('submit')
    await flushPromises()
    const [m, url, body] = apiSend.mock.calls[0]
    expect(m).toBe('POST')
    expect(url).toBe('/api/credentials')
    expect(body).toMatchObject({
      name: 'Acme login', kind: 'login', login_url: 'https://acme.test/login', username: 'alice', secret: 'hunter22pw',
      selectors: { username: '#email', code: '#otp' }, extra_hosts: ['app.acme.test', 'api.acme.test', 'cdn.acme.test'],
    })
    expect(body.selectors.password).toBeUndefined()
    w.unmount()
  })

  it('edit keeps password blank and shows the hint', async () => {
    const w = mount(KeyForm, { props: { keyItem: loginKey({ extra_hosts: ['a.test'] }) as never }, attachTo: document.body })
    expect(w.text()).toContain('Leave blank to keep the current password')
    expect((w.get('#kf-hosts').element as HTMLInputElement).value).toBe('a.test')
    apiSend.mockResolvedValue({})
    await w.get('form').trigger('submit')
    await flushPromises()
    expect(apiSend.mock.calls[0][0]).toBe('PATCH')
    expect(apiSend.mock.calls[0][2].secret).toBeUndefined()
    w.unmount()
  })
})

describe('target form with a login key', () => {
  it('target form forces browser for login key', async () => {
    apiGet.mockResolvedValue([loginKey(), { ...loginKey({ id: 1, name: 'Tok', kind: 'header' }), login_status: undefined }])
    const w = mount(TargetForm, { props: { sourceNames: [] }, attachTo: document.body })
    await flushPromises()
    const box = () => w.get('[data-test=browser-box]').element as HTMLInputElement
    expect(box().disabled).toBe(false)
    await w.get('[data-test=key-select]').setValue(7)
    expect(box().checked).toBe(true)
    expect(box().disabled).toBe(true)
    expect(w.text()).toContain('Logins always use the browser')
    expect(w.get('[data-test=key-problem]').text()).not.toContain("can't be used with")
    await w.get('#tf-source').setValue('S')
    await w.get('#tf-kpi').setValue('K')
    await w.get('#tf-url').setValue('https://other.test/x')
    apiSend.mockRejectedValue(new ApiError(422, 'this login is for acme.test; add the KPI\'s site under "Also used on"'))
    await w.get('form').trigger('submit')
    await flushPromises()
    expect(w.get('[role=alert]').text()).toContain('Also used on')
    expect(apiSend.mock.calls[0][2].use_browser).toBe(true)
    await w.get('[data-test=key-select]').setValue(1)
    expect(box().disabled).toBe(false)
    expect(box().checked).toBe(false) // back to what it was before the login forced it
    w.unmount()
  })
})

describe('StatusBadge', () => {
  it('StatusBadge login_needed', () => {
    const w = mount(StatusBadge, { props: { status: 'login_needed' } })
    expect(w.text()).toContain('Login needed')
    expect(w.classes()).toContain('tone-warn')
    expect(w.get('[data-icon]').attributes('data-icon-name')).toBe('lock')
  })
})

describe('KeysCard sign-in button', () => {
  it('is disabled when the key has no readable secret', async () => {
    apiGet.mockResolvedValue([loginKey({ readable: false }), loginKey({ id: 8, has_secret: false })])
    const w = mount(KeysCard, { attachTo: document.body })
    await flushPromises()
    expect((w.get('[data-test=signin-7]').element as HTMLButtonElement).disabled).toBe(true)
    expect((w.get('[data-test=signin-8]').element as HTMLButtonElement).disabled).toBe(true)
    w.unmount()
  })

  it('returns focus to the Sign in now button when the dialog closes', async () => {
    apiGet.mockImplementation(async (u: string) => (u.includes('attempt') ? at('signing_in') : [loginKey()]))
    apiSend.mockResolvedValue({ attempt_id: 5, status: 'pending' })
    const w = mount(KeysCard, { attachTo: document.body })
    await flushPromises()
    const btn = w.get('[data-test=signin-7]').element as HTMLButtonElement
    btn.focus()
    await w.get('[data-test=signin-7]').trigger('click')
    await flushPromises()
    expect(document.activeElement).not.toBe(btn)
    ;(document.querySelector('[data-test=cancel]') as HTMLButtonElement).click()
    await flushPromises()
    expect(document.querySelector('[role=dialog]')).toBeNull()
    expect(document.activeElement).toBe(btn)
    w.unmount()
  })
})

describe('KeysCard login rows', () => {
  it('shows status and opens the dialog', async () => {
    apiGet.mockResolvedValue([loginKey(), loginKey({ id: 8, name: 'B', login_status: 'failed', login_error: 'wrong password' }),
      loginKey({ id: 9, name: 'C', login_status: 'ok' }), loginKey({ id: 10, name: 'D', login_status: 'waiting_code' })])
    const w = mount(KeysCard, { attachTo: document.body })
    await flushPromises()
    expect(w.get('[data-test=key-7]').text()).toContain('Login needed')
    // 'failed' is never written for a login (a failed sign-in leaves it login_needed); shown as such.
    expect(w.get('[data-test=key-8]').text()).toContain('Login needed')
    expect(w.get('[data-test=key-8]').text()).not.toContain('Sign-in failed')
    expect(w.get('[data-test=key-8]').text()).toContain('wrong password')
    expect(w.get('[data-test=key-9]').text()).toContain('Signed in')
    expect(w.get('[data-test=key-10]').text()).toContain('Signing in…')
    expect((w.get('[data-test=signin-10]').element as HTMLButtonElement).disabled).toBe(true)
    expect(w.get('section').attributes('id')).toBe('logins')
    apiSend.mockResolvedValue({ attempt_id: 5, status: 'pending' })
    await w.get('[data-test=signin-7]').trigger('click')
    await flushPromises()
    expect(apiSend).toHaveBeenCalledWith('POST', '/api/logins/7/sign-in')
    expect(document.body.textContent).toContain('Signing in to acme.test')
    w.unmount()
  })
})

const at = (status: string, extra: Record<string, unknown> = {}) =>
  ({ attempt_id: 5, status, error: null, waiting_since: null, expires_at: null, remaining_seconds: null, ...extra })

async function openDialog() {
  const w = mount(SignInDialog, { props: { open: true, credentialId: 7, keyName: 'Acme login', host: 'acme.test' }, attachTo: document.body })
  await flushPromises()
  return w
}
const txt = () => document.body.textContent ?? ''

describe('SignInDialog', () => {
  it('sign-in dialog full flow with countdown', async () => {
    vi.useFakeTimers({ now: new Date('2026-11-26T10:00:00Z') })
    apiSend.mockResolvedValueOnce({ attempt_id: 5, status: 'pending' })
    let current = at('signing_in')
    const serverExp = Date.parse('2026-11-26T10:10:00Z')
    apiGet.mockImplementation(async () => (current.status === 'waiting_code' ? { ...current, remaining_seconds: Math.max(0, Math.ceil((serverExp - Date.now()) / 1000)) } : current))
    const w = await openDialog()
    expect(txt()).toContain('Signing in to acme.test…')
    current = at('waiting_code', { waiting_since: '2026-11-26T10:00:00Z', expires_at: '2026-11-26T10:10:00Z', remaining_seconds: 590 })
    await vi.advanceTimersByTimeAsync(2100)
    expect(txt()).toContain('We sent a code to your email. Enter it within 09:5')
    const input = document.querySelector('input[autocomplete=one-time-code]') as HTMLInputElement
    expect(input).toBeTruthy()
    expect(input.getAttribute('inputmode')).toBe('numeric')
    expect(input.getAttribute('maxlength')).toBe('64')
    await vi.advanceTimersByTimeAsync(60_000)
    expect(txt()).toContain('Enter it within 08:5')
    // submit
    apiSend.mockResolvedValueOnce({ attempt_id: 5, status: 'waiting_code' })
    input.value = '123456'
    input.dispatchEvent(new Event('input'))
    await flushPromises()
    ;(document.querySelector('[data-test=code-submit]') as HTMLButtonElement).click()
    await flushPromises()
    expect(apiSend).toHaveBeenLastCalledWith('POST', '/api/logins/attempts/5/code', { code: '123456' })
    expect(txt()).toContain('Checking the code…')
    current = at('verifying')
    await vi.advanceTimersByTimeAsync(2100)
    expect(txt()).toContain('Checking the code…')
    current = at('succeeded')
    await vi.advanceTimersByTimeAsync(2100)
    expect(txt()).toContain('Signed in. Checking your KPIs now.')
    expect(w.emitted('done')).toHaveLength(1)
    // polling stops at a final state
    const n = apiGet.mock.calls.length
    await vi.advanceTimersByTimeAsync(10_000)
    expect(apiGet.mock.calls.length).toBe(n)
    w.unmount()
  })

  it('expired shows try again', async () => {
    vi.useFakeTimers({ now: new Date('2026-11-26T10:00:00Z') })
    apiSend.mockResolvedValueOnce({ attempt_id: 5, status: 'pending' })
    // The server never flips: expires_at alone decides.
    const serverExp = Date.parse('2026-11-26T10:00:02Z')
    apiGet.mockImplementation(async () => at('waiting_code', { expires_at: '2026-11-26T10:00:02Z', remaining_seconds: Math.max(0, Math.ceil((serverExp - Date.now()) / 1000)) }))
    const w = await openDialog()
    await vi.advanceTimersByTimeAsync(2100)
    const calls = apiGet.mock.calls.length
    await vi.advanceTimersByTimeAsync(4100)
    expect(apiGet.mock.calls.length).toBeGreaterThan(calls) // polling goes on: the server status wins
    expect(txt()).toContain('this sign-in expired — click Sign in now again')
    expect(document.querySelector('input[autocomplete=one-time-code]')).toBeNull()
    const retry = Array.from(document.querySelectorAll('button')).find((b) => b.textContent?.includes('Try again'))!
    expect(retry).toBeTruthy()
    apiSend.mockResolvedValue({ attempt_id: 6, status: 'pending' })
    retry.click()
    await flushPromises()
    expect(apiSend).toHaveBeenCalledWith('POST', '/api/logins/attempts/5/cancel')
    expect(apiSend).toHaveBeenLastCalledWith('POST', '/api/logins/7/sign-in')
    w.unmount()
  })

  it('failed shows the server error and try again', async () => {
    apiSend.mockResolvedValueOnce({ attempt_id: 5, status: 'pending' })
    apiGet.mockResolvedValue(at('failed', { error: 'wrong password' }))
    const w = await openDialog()
    await flushPromises()
    expect(txt()).toContain('wrong password')
    expect(txt()).toContain('Try again')
    w.unmount()
  })

  it('code input cleared after submit', async () => {
    apiSend.mockResolvedValueOnce({ attempt_id: 5, status: 'pending' })
    apiGet.mockResolvedValue(at('waiting_code', { expires_at: new Date(Date.now() + 600_000).toISOString(), remaining_seconds: 600 }))
    const w = await openDialog()
    await flushPromises()
    const input = document.querySelector('input[autocomplete=one-time-code]') as HTMLInputElement
    input.value = 'ab-12'
    input.dispatchEvent(new Event('input'))
    await flushPromises()
    apiSend.mockResolvedValueOnce({ attempt_id: 5, status: 'waiting_code' })
    ;(document.querySelector('[data-test=code-submit]') as HTMLButtonElement).click()
    await flushPromises()
    expect(input.value).toBe('')
    expect(document.body.innerHTML).not.toContain('ab-12')
    // invalid characters are blocked client-side with the server's wording
    apiSend.mockClear()
    w.unmount()
  })

  it('rejects bad characters before sending', async () => {
    apiSend.mockResolvedValueOnce({ attempt_id: 5, status: 'pending' })
    apiGet.mockResolvedValue(at('waiting_code', { expires_at: new Date(Date.now() + 600_000).toISOString(), remaining_seconds: 600 }))
    const w = await openDialog()
    await flushPromises()
    const input = document.querySelector('input[autocomplete=one-time-code]') as HTMLInputElement
    input.value = '12 34!'
    input.dispatchEvent(new Event('input'))
    await flushPromises()
    apiSend.mockClear()
    ;(document.querySelector('[data-test=code-submit]') as HTMLButtonElement).click()
    await flushPromises()
    expect(apiSend).not.toHaveBeenCalled()
    expect(txt()).toContain('the code can only contain letters, digits and dashes')
    w.unmount()
  })

  it('a slow start that fails still shows the error and starts no polling', async () => {
    vi.useFakeTimers({ now: new Date('2026-11-26T10:00:00Z') })
    let fail!: () => void
    apiSend.mockImplementationOnce(() => new Promise((_, rej) => { fail = () => rej(new ApiError(503, 'keys unavailable')) }))
    apiGet.mockResolvedValue(at('failed', { error: 'old attempt' }))
    const w = await openDialog()
    await vi.advanceTimersByTimeAsync(5000)
    expect(apiGet).not.toHaveBeenCalled()
    fail()
    await vi.advanceTimersByTimeAsync(0)
    expect(txt()).toContain('keys unavailable')
    expect(txt()).not.toContain('old attempt')
    await vi.advanceTimersByTimeAsync(6000)
    expect(apiGet).not.toHaveBeenCalled()
    w.unmount()
  })

  it('never overlaps polls', async () => {
    vi.useFakeTimers({ now: new Date('2026-11-26T10:00:00Z') })
    apiSend.mockResolvedValueOnce({ attempt_id: 5, status: 'pending' })
    apiGet.mockImplementation(() => new Promise(() => {}))
    const w = await openDialog()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(apiGet).toHaveBeenCalledTimes(1)
    w.unmount()
  })

  it('a rejected code that returns to waiting_code lets you type again', async () => {
    vi.useFakeTimers({ now: new Date('2026-11-26T10:00:00Z') })
    apiSend.mockResolvedValueOnce({ attempt_id: 5, status: 'pending' })
    let cur = at('waiting_code', { waiting_since: '2026-11-26T10:00:00Z', remaining_seconds: 600 })
    apiGet.mockImplementation(async () => cur)
    const w = await openDialog()
    await vi.advanceTimersByTimeAsync(0)
    const input = document.querySelector('input[autocomplete=one-time-code]') as HTMLInputElement
    input.value = '111111'
    input.dispatchEvent(new Event('input'))
    await vi.advanceTimersByTimeAsync(0)
    apiSend.mockResolvedValueOnce({ attempt_id: 5, status: 'waiting_code' })
    ;(document.querySelector('[data-test=code-submit]') as HTMLButtonElement).click()
    await vi.advanceTimersByTimeAsync(0)
    expect(txt()).toContain('Checking the code…')
    cur = at('waiting_code', { waiting_since: '2026-11-26T10:00:03Z', remaining_seconds: 597 })
    await vi.advanceTimersByTimeAsync(2100)
    expect(document.querySelector('input[autocomplete=one-time-code]')).not.toBeNull()
    w.unmount()
  })

  it('clicking the backdrop does not cancel while waiting for the code; Escape does', async () => {
    apiSend.mockResolvedValueOnce({ attempt_id: 5, status: 'pending' })
    apiGet.mockResolvedValue(at('waiting_code', { remaining_seconds: 600, expires_at: new Date(Date.now() + 600_000).toISOString() }))
    const w = await openDialog()
    await flushPromises()
    ;(document.querySelector('.overlay') as HTMLElement).click()
    await flushPromises()
    expect(w.emitted('close')).toBeUndefined()
    apiSend.mockResolvedValueOnce({})
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    expect(w.emitted('close')).toHaveLength(1)
    w.unmount()
  })

  it('adopts the running attempt on 409', async () => {
    apiSend.mockRejectedValueOnce(new ApiError(409, 'a sign-in is already in progress'))
    apiGet.mockResolvedValue(at('signing_in'))
    const w = await openDialog()
    expect(txt()).toContain('Signing in to acme.test…')
    expect(txt()).not.toContain('already in progress')
    w.unmount()
  })

  it('adopting a finished attempt on 409 starts no timers', async () => {
    vi.useFakeTimers({ now: new Date('2026-11-26T10:00:00Z') })
    apiSend.mockRejectedValueOnce(new ApiError(409, 'a sign-in is already in progress'))
    apiGet.mockResolvedValue(at('failed', { error: 'login failed: still on the login page' }))
    const intervals = vi.spyOn(globalThis, 'setInterval')
    const w = await openDialog()
    expect(txt()).toContain('login failed: still on the login page')
    expect(intervals).not.toHaveBeenCalled()
    intervals.mockRestore()
    w.unmount()
  })

  it('cancel while signing in says cancelling and discards', async () => {
    apiSend.mockResolvedValueOnce({ attempt_id: 5, status: 'pending' })
    apiGet.mockResolvedValue(at('verifying'))
    const w = await openDialog()
    let release!: () => void
    apiSend.mockImplementationOnce(() => new Promise((r) => { release = () => r({}) }))
    ;(document.querySelector('[data-test=cancel]') as HTMLButtonElement).click()
    await flushPromises()
    expect(txt()).toContain('Cancelling… the browser finishes its current step')
    expect(txt()).toContain('If the sign-in was already accepted it will be discarded.')
    release()
    await flushPromises()
    expect(w.emitted('close')).toHaveLength(1)
    w.unmount()
  })
})

function src(over: Partial<Source>): Source {
  return {
    slug: 's', name: 'S', area: 'Macro', access: 'watch', enabled: true, cron: null,
    last_run: { id: 1, status: 'ok', finished_at: '2026-11-20T10:00:00Z', added: 0, altered: 0, removed: 0 },
    last_change_at: null, rows_tracked: 1, changes_30d: 0, failing_streak: 0, attribution: null, incomplete: false,
    noisy_targets: 0, logins_needed: 0, last_success_at: null, credential_id: null, credential_name: null, credential_supported: false, ...over,
  }
}

describe('Overview', () => {
  it('overview shows sign-in needed', async () => {
    apiGet.mockResolvedValue([src({ slug: 'a', name: 'Alpha' }), src({ slug: 'b', name: 'Beta', logins_needed: 2 })])
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: { template: '<div/>' } }] })
    const w = mount(OverviewPage, { global: { plugins: [router] } })
    await flushPromises()
    const link = w.get('a[href="/settings#logins"]')
    expect(link.text()).toContain('Sign-in needed')
    expect(w.findAll('a[href="/settings#logins"]')).toHaveLength(1)
    expect(w.findAll('[data-test=source-name]')[0].text()).toBe('Beta')
    expect(w.get('[data-test=stat-attention]').text()).toContain('1')
  })
})

describe('Settings #logins', () => {
  it('scrolls to the Keys card once it has loaded, and on a later hash change', async () => {
    const { default: SettingsPage } = await import('../pages/SettingsPage.vue')
    let release!: (v: unknown) => void
    apiGet.mockImplementation((p: string) => {
      if (p === '/api/credentials') return new Promise((r) => { release = r })
      if (p === '/api/credentials/status') return Promise.resolve({ configured: true })
      return Promise.resolve([])
    })
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    window.history.replaceState(null, '', '/settings#logins')
    const w = mount(SettingsPage, { attachTo: document.body })
    await flushPromises()
    const early = scroll.mock.calls.length
    release([loginKey()])
    await flushPromises()
    expect(scroll.mock.calls.length).toBe(early + 1) // again, now that the rows are there
    expect(scroll.mock.contexts.at(-1)).toBe(document.getElementById('logins'))
    window.history.replaceState(null, '', '/settings')
    window.history.replaceState(null, '', '/settings#logins')
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await flushPromises()
    expect(scroll.mock.calls.length).toBe(early + 2)
    w.unmount()
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await flushPromises()
    expect(scroll.mock.calls.length).toBe(early + 2) // listener removed
    window.history.replaceState(null, '', '/')
  })
})

describe('SignInDialog focus and announcements (plan 3a task 5)', () => {
  it('does not restore focus to document.body; falls back to main', async () => {
    const main = document.createElement('main')
    main.tabIndex = -1
    document.body.appendChild(main)
    ;(document.activeElement as HTMLElement | null)?.blur()
    expect(document.activeElement).toBe(document.body)
    apiSend.mockResolvedValueOnce({ attempt_id: 5, status: 'pending' })
    apiGet.mockResolvedValue(at('signing_in'))
    const w = await openDialog()
    await w.setProps({ open: false })
    await flushPromises()
    expect(document.activeElement).toBe(main)
    w.unmount()
  })

  it('a backdrop click closes the locally expired view', async () => {
    vi.useFakeTimers({ now: new Date('2026-11-26T10:00:00Z') })
    apiSend.mockResolvedValueOnce({ attempt_id: 5, status: 'pending' })
    const serverExp = Date.parse('2026-11-26T10:00:02Z')
    apiGet.mockImplementation(async () => at('waiting_code', { expires_at: '2026-11-26T10:00:02Z', remaining_seconds: Math.max(0, Math.ceil((serverExp - Date.now()) / 1000)) }))
    const w = await openDialog()
    await vi.advanceTimersByTimeAsync(4100)
    expect(txt()).toContain('this sign-in expired')
    ;(document.querySelector('.overlay') as HTMLElement).click()
    await flushPromises()
    expect(w.emitted('close')).toHaveLength(1)
    w.unmount()
  })

  it('the problem text is announced once: no role=alert inside the live region', async () => {
    apiSend.mockResolvedValueOnce({ attempt_id: 5, status: 'pending' })
    apiGet.mockResolvedValue(at('failed', { error: 'wrong password' }))
    const w = await openDialog()
    await flushPromises()
    const live = document.querySelector('[aria-live]') as HTMLElement
    expect(live.textContent).toContain('wrong password')
    expect(live.querySelector('[role=alert]')).toBeNull()
    w.unmount()
  })
})
