import { beforeEach, describe, expect, it } from 'vitest'
import router from '../router'
import { clearPassword, setPassword } from '../api/client'

describe('router', () => {
  beforeEach(async () => {
    clearPassword()
    await router.replace('/login')
  })

  it('redirects an unauthenticated visit to /kpis to /login?next=/kpis', async () => {
    await router.push('/kpis')
    expect(router.currentRoute.value.fullPath).toBe('/login?next=/kpis')
  })

  it('lets an authenticated visit through', async () => {
    setPassword('p')
    await router.push('/kpis')
    expect(router.currentRoute.value.path).toBe('/kpis')
  })

  it('sends an authenticated visit to /login on to next, or /', async () => {
    setPassword('p')
    await router.push('/login?next=/changes')
    expect(router.currentRoute.value.path).toBe('/changes')
    await router.push('/login')
    expect(router.currentRoute.value.path).toBe('/')
  })

  it('declares every route in the brief', () => {
    const paths = router.getRoutes().map((r) => r.path)
    for (const p of ['/login', '/', '/changes', '/runs/:id', '/explorer/:slug?/:indicator?/:country?', '/kpis', '/uploads', '/settings'])
      expect(paths).toContain(p)
  })
})
