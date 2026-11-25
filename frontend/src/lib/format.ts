const nf = new Intl.NumberFormat('en-US')
export const fmtNum = (n: number): string => nf.format(n)

export function relativeTime(iso: string | null | undefined, empty = '—'): string {
  if (!iso) return empty
  const diff = (new Date(iso).getTime() - Date.now()) / 1000
  const units: [Intl.RelativeTimeFormatUnit, number][] = [['day', 86400], ['hour', 3600], ['minute', 60]]
  const rtf = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })
  for (const [u, s] of units) if (Math.abs(diff) >= s) return rtf.format(Math.round(diff / s), u)
  return 'just now'
}

export const absoluteTime = (iso: string | null | undefined): string => (iso ? new Date(iso).toLocaleString() : '')

export function fmtDims(dims: Record<string, unknown> | null | undefined): string {
  if (!dims) return '—'
  const entries = Object.entries(dims)
  return entries.length ? entries.map(([k, v]) => `${k}=${String(v)}`).join(', ') : '—'
}

/** Numeric strings with >=5 integer digits get thousands separators; years/codes stay raw. */
export function fmtValue(v: string | null | undefined): string {
  if (v === null || v === undefined) return '—'
  const m = /^(-?)(\d{5,})(\.\d+)?$/.exec(v)
  if (!m) return v
  return m[1] + m[2].replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (m[3] ?? '')
}
