import router from '../router'
import { clearPassword, getPassword, hasPassword, setLoginMessage, setPassword } from './auth'

export { clearPassword, hasPassword, setPassword }

export class ApiError extends Error {
  status: number
  detail: string
  /** Parsed JSON error body (e.g. the used_by list of a 409), when there was one. */
  body: unknown
  constructor(status: number, detail: string, body: unknown = null) {
    super(detail)
    this.body = body
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

type Params = Record<string, string | number | boolean | null | undefined>

function authHeader(): Record<string, string> {
  const pw = getPassword()
  if (pw === null) return {}
  const bytes = new TextEncoder().encode('dashboard:' + pw)
  let bin = ''
  bytes.forEach((b) => (bin += String.fromCharCode(b)))
  return { Authorization: 'Basic ' + btoa(bin) }
}

function withParams(path: string, params?: Params): string {
  if (!params) return path
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') qs.set(k, String(v))
  }
  const s = qs.toString()
  return s ? `${path}${path.includes('?') ? '&' : '?'}${s}` : path
}

function detailOf(body: unknown, fallback: string): string {
  const d = (body as { detail?: unknown } | null)?.detail
  if (typeof d === 'string') return d
  if (Array.isArray(d)) {
    return d.map((e) => (e && typeof e === 'object' && 'msg' in e ? String((e as { msg: unknown }).msg) : String(e))).join('; ')
  }
  return fallback
}

function handleUnauthorized(): void {
  clearPassword()
  setLoginMessage('Password incorrect or expired')
  const cur = router.currentRoute.value
  if (cur.path !== '/login') void router.replace({ path: '/login', query: { next: cur.fullPath } })
}

/** CSRF guard: the API refuses unsafe methods without it (a cross-site form cannot set it). */
const REQUESTED_WITH = { 'X-Requested-With': 'kpi-monitor' }

async function request<T>(url: string, init: RequestInit & { headers: Record<string, string> }): Promise<T> {
  let res: Response
  try {
    res = await fetch(url, { ...init, headers: { ...authHeader(), ...REQUESTED_WITH, ...init.headers } })
  } catch {
    throw new ApiError(0, 'Network error — check your connection')
  }
  if (res.status === 401) {
    handleUnauthorized()
    throw new ApiError(401, 'Password incorrect or expired')
  }
  const text = await res.text()
  let body: unknown = null
  if (text) {
    try {
      body = JSON.parse(text)
    } catch {
      body = text
    }
  }
  if (!res.ok) throw new ApiError(res.status, detailOf(body, res.statusText || `HTTP ${res.status}`), body)
  return body as T
}

export function apiGet<T>(path: string, params?: Params, signal?: AbortSignal): Promise<T> {
  return request<T>(withParams(path, params), { method: 'GET', headers: { Accept: 'application/json' }, signal })
}

export function apiSend<T>(method: 'POST' | 'PATCH' | 'PUT' | 'DELETE', path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  const init: RequestInit & { headers: Record<string, string> } = { method, headers }
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(body)
  }
  return request<T>(path, init)
}

export function apiUpload<T>(path: string, formData: FormData): Promise<T> {
  // No Content-Type: the browser sets the multipart boundary.
  return request<T>(path, { method: 'POST', headers: { Accept: 'application/json' }, body: formData })
}
