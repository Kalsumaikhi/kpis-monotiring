import { describe, expect, it } from 'vitest'
import { safeNext } from '../router/safeNext'

describe('safeNext', () => {
  it.each([
    ['/x', '/x'],
    ['/x?a=1#h', '/x?a=1#h'],
    ['//evil.com', '/'],
    ['/\\evil.com', '/'],
    ['/\t/evil.com', '/'],
    ['https://evil.com', '/'],
    ['/login', '/'],
    ['/login?next=/x', '/'],
    ['', '/'],
  ])('%j -> %j', (input, expected) => {
    expect(safeNext(input)).toBe(expected)
  })
  it('handles non-strings and arrays', () => {
    expect(safeNext(undefined)).toBe('/')
    expect(safeNext(['/kpis', '/x'])).toBe('/kpis')
  })
})
