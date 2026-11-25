<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{ open: boolean; title: string; message?: string; confirmLabel?: string; cancelLabel?: string; danger?: boolean }>(),
  { confirmLabel: 'Confirm', cancelLabel: 'Cancel', danger: false },
)
const emit = defineEmits<{ confirm: []; cancel: [] }>()
const cancelBtn = ref<HTMLButtonElement | null>(null)
const dialog = ref<HTMLElement | null>(null)
let opener: HTMLElement | null = null

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.preventDefault()
    emit('cancel')
    return
  }
  if (e.key !== 'Tab' || !dialog.value) return
  const items = Array.from(dialog.value.querySelectorAll<HTMLElement>(FOCUSABLE))
  if (!items.length) return
  const first = items[0]
  const last = items[items.length - 1]
  const active = document.activeElement as HTMLElement | null
  if (!active || !dialog.value.contains(active)) {
    e.preventDefault()
    first.focus()
  } else if (e.shiftKey && active === first) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && active === last) {
    e.preventDefault()
    first.focus()
  }
}

watch(
  () => props.open,
  async (o) => {
    if (o) {
      opener = document.activeElement as HTMLElement | null
      document.addEventListener('keydown', onKey)
      await nextTick()
      cancelBtn.value?.focus() // the safe choice
    } else {
      document.removeEventListener('keydown', onKey)
      opener?.focus()
      opener = null
    }
  },
)
onBeforeUnmount(() => document.removeEventListener('keydown', onKey))
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="overlay" @click.self="emit('cancel')">
      <div ref="dialog" class="dialog" role="dialog" aria-modal="true" aria-labelledby="cd-title" :aria-describedby="message ? 'cd-msg' : undefined">
        <h2 id="cd-title">{{ title }}</h2>
        <p v-if="message" id="cd-msg" class="muted">{{ message }}</p>
        <slot />
        <div class="actions">
          <button ref="cancelBtn" class="btn secondary" type="button" @click="emit('cancel')">{{ cancelLabel }}</button>
          <button class="btn" :class="{ danger }" type="button" @click="emit('confirm')">{{ confirmLabel }}</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.overlay { position: fixed; inset: 0; background: var(--overlay); display: flex; align-items: center; justify-content: center; padding: 16px; z-index: 50; }
.dialog { background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px; width: min(440px, 100%); display: flex; flex-direction: column; gap: 12px; }
.actions { display: flex; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }
</style>
