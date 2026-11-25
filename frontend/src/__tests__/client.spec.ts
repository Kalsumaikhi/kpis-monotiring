import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, apiGet, apiSend, clearPassword, hasPassword, setPassword } from '../api/client'
import router from '../router'

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn().mockResolvedValue(
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }),
  )
  vi.stubGlobal('fetch', fn)
  return fn
}

describe('api client', () => {
  beforeEach(async () => {
    sessionStorage.clear()
    clearPassword()
    await router.replace('/login')
  })

  it('api client adds basic auth header', async () => {
    setPassword('s3cret')
    const fn = mockFetch(200, [])
    await apiGet('/api/sources', { page: 2, q: undefined })
    const [url, init] = fn.mock.calls[0]
    expect(url).toBe('/api/sources?page=2')
    expect(init.headers.Authorization).toBe('Basic ' + btoa('dashboard:s3cret'))
  })

  it('api client clears password and redirects on 401', async () => {
    setPassword('old')
    await router.replace('/kpis').catch(() => undefined)
    mockFetch(401, { detail: 'nope' })
    await expect(apiGet('/api/sources')).rejects.toBeInstanceOf(ApiError)
    expect(hasPassword()).toBe(false)
    await vi.waitFor(() => expect(router.currentRoute.value.path).toBe('/login'))
    expect(sessionStorage.getItem('loginMessage')).toBe('Password incorrect or expired')
  })

  it('api client throws ApiError with detail on 409', async () => {
    setPassword('p')
    mockFetch(409, { detail: 'source already has a queued or running run' })
    const err = (await apiSend('POST', '/api/sources/x/run').catch((e) => e)) as ApiError
    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(409)
    expect(err.detail).toBe('source already has a queued or running run')
  })

  it('wraps a network failure in ApiError(0)', async () => {
    setPassword('p')
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    const err = (await apiGet('/api/sources').catch((e) => e)) as ApiError
    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(0)
    expect(err.detail).toBe('Network error — check your connection')
  })
})
