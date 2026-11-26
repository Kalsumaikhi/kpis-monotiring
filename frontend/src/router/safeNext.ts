/** Sanitise a post-login redirect target: same-origin, not the login page; else '/'. */
export function safeNext(value: unknown): string {
  const raw = Array.isArray(value) ? value[0] : value
  if (typeof raw !== 'string' || !raw) return '/'
  try {
    const origin = window.location.origin
    const u = new URL(raw, origin)
    if (u.origin !== origin) return '/'
    if (u.pathname === '/login' || u.pathname.startsWith('/login/')) return '/'
    const out = u.pathname + u.search + u.hash
    // Dot segments can normalise into a protocol-relative path ('/.//evil.com' -> '//evil.com'),
    // which a later navigation would read as another host. Keep only a stable same-origin path.
    if (out.startsWith('//') || new URL(out, origin).origin !== origin) return '/'
    return out
  } catch {
    return '/'
  }
}
