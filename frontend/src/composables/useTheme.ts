import { ref } from 'vue'

export type ThemeChoice = 'system' | 'light' | 'dark'
const KEY = 'theme'
export const theme = ref<ThemeChoice>('system')

function read(): ThemeChoice {
  try {
    const v = window.localStorage.getItem(KEY)
    if (v === 'light' || v === 'dark') return v
  } catch {
    /* storage unavailable */
  }
  return 'system'
}

function apply(t: ThemeChoice): void {
  const el = document.documentElement
  if (t === 'system') el.removeAttribute('data-theme')
  else el.setAttribute('data-theme', t)
}

export function initTheme(): void {
  theme.value = read()
  apply(theme.value)
}

export function setTheme(t: ThemeChoice): void {
  theme.value = t
  apply(t)
  try {
    if (t === 'system') window.localStorage.removeItem(KEY)
    else window.localStorage.setItem(KEY, t)
  } catch {
    /* the choice still applies for this page view */
  }
}
