import type { StoredKey } from '../api/types'
import { apiGet } from '../api/client'

/** Keys for a "Key" dropdown. Never throws: on any failure the dropdown just offers "None". */
export async function fetchKeys(): Promise<StoredKey[]> {
  try {
    const res = await apiGet<StoredKey[]>('/api/credentials')
    return Array.isArray(res) ? res.filter((k) => k && typeof k === 'object' && 'has_secret' in k) : []
  } catch {
    return []
  }
}
