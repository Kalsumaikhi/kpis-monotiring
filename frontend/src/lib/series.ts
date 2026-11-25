import type { SeriesPoint } from '../api/types'

export const MAX_SERIES = 6

export interface SeriesGroup {
  key: string
  label: string
  values: Map<string, string | null>
}

/** A series is numeric when every non-null value is a finite number (and at least one exists). */
export function isNumericSeries(points: SeriesPoint[]): boolean {
  let any = false
  for (const p of points) {
    if (p.value === null) continue
    if (p.value.trim() === '' || !Number.isFinite(Number(p.value))) return false
    any = true
  }
  return any
}

/** Years sort numerically; "2024-Q3" / "2024-05" sort lexicographically; anything mixed keeps the given order. */
export function sortPeriods(periods: string[]): string[] {
  const uniq = [...new Set(periods)]
  if (uniq.every((p) => /^\d+$/.test(p))) return uniq.sort((a, b) => Number(a) - Number(b))
  if (uniq.every((p) => /^\d{4}(-(Q[1-4]|\d{2}(-\d{2})?))?$/.test(p))) {
    const kinds = new Set(uniq.map((p) => p.replace(/\d/g, '9')))
    if (kinds.size === 1) return uniq.sort()
  }
  return uniq
}

export function dimsLabel(dims: Record<string, unknown> | null): string {
  if (!dims) return ''
  const e = Object.entries(dims)
  return e.length ? e.map(([k, v]) => `${k}=${String(v)}`).join(', ') : ''
}

/** One group per distinct dims set, in order of first appearance. */
export function groupSeries(points: SeriesPoint[]): SeriesGroup[] {
  const groups = new Map<string, SeriesGroup>()
  for (const p of points) {
    const label = dimsLabel(p.dims)
    let g = groups.get(label)
    if (!g) groups.set(label, (g = { key: label, label: label || 'Value', values: new Map() }))
    g.values.set(p.period, p.value)
  }
  return [...groups.values()]
}
