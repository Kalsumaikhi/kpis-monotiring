/** Sanitise a post-login redirect target: same-origin, not the login page; else '/'. */
export function safeNext(value: unknown): string {
  const raw = Array.isArray(value) ? value[0] : value
  if (typeof raw !== 'string' || !raw) return '/'
  try {
    const u = new URL(raw, window.location.origin)
    if (u.origin !== window.location.origin) return '/'
    if (u.pathname === '/login' || u.pathname.startsWith('/login/')) return '/'
    return u.pathname + u.search + u.hash
  } catch {
    return '/'
  }
}
