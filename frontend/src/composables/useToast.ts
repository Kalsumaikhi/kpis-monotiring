import { ref } from 'vue'

export interface Toast { id: number; message: string; tone: 'info' | 'error' }

export const toasts = ref<Toast[]>([])
let nextId = 1
const timers = new Map<number, ReturnType<typeof setTimeout>>()

export function dismissToast(id: number): void {
  const t = timers.get(id)
  if (t !== undefined) clearTimeout(t)
  timers.delete(id)
  toasts.value = toasts.value.filter((x) => x.id !== id)
}

export function useToast() {
  /** Info toasts auto-dismiss after 5 s; error toasts stay until dismissed (pass ms to override). */
  function show(message: string, tone: Toast['tone'] = 'info', ms = tone === 'error' ? 0 : 5000): void {
    const id = nextId++
    toasts.value = [...toasts.value, { id, message, tone }]
    if (ms > 0) timers.set(id, setTimeout(() => dismissToast(id), ms))
  }
  return { toasts, show, dismiss: dismissToast }
}
