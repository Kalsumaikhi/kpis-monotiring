import { ref } from 'vue'

export interface Toast { id: number; message: string; tone: 'info' | 'error' }

export const toasts = ref<Toast[]>([])
let nextId = 1

export function dismissToast(id: number): void {
  toasts.value = toasts.value.filter((t) => t.id !== id)
}

export function useToast() {
  function show(message: string, tone: Toast['tone'] = 'info', ms = 5000): void {
    const id = nextId++
    toasts.value = [...toasts.value, { id, message, tone }]
    if (ms > 0) setTimeout(() => dismissToast(id), ms)
  }
  return { toasts, show, dismiss: dismissToast }
}
