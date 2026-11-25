// Password storage kept apart from the client so the router can use it without an import cycle.
const KEY = 'dashboardPassword'
let memory: string | null = null

function store(): Storage | null {
  try {
    return window.sessionStorage
  } catch {
    return null
  }
}

export function setPassword(p: string): void {
  memory = p
  try {
    store()?.setItem(KEY, p)
  } catch {
    /* storage blocked: the in-memory copy keeps this tab working */
  }
}

export function clearPassword(): void {
  memory = null
  try {
    store()?.removeItem(KEY)
  } catch {
    /* ignore */
  }
}

export function getPassword(): string | null {
  if (memory) return memory
  try {
    return store()?.getItem(KEY) ?? null
  } catch {
    return null
  }
}

export function hasPassword(): boolean {
  return !!getPassword()
}

export const LOGIN_MESSAGE_KEY = 'loginMessage'

export function setLoginMessage(msg: string): void {
  try {
    store()?.setItem(LOGIN_MESSAGE_KEY, msg)
  } catch {
    /* ignore */
  }
}

/** Read and remove the message so it shows once. */
export function takeLoginMessage(): string | null {
  try {
    const s = store()
    const m = s?.getItem(LOGIN_MESSAGE_KEY) ?? null
    s?.removeItem(LOGIN_MESSAGE_KEY)
    return m
  } catch {
    return null
  }
}
