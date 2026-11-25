<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{ open: boolean; title: string; message?: string; confirmLabel?: string; cancelLabel?: string; danger?: boolean }>(),
  { confirmLabel: 'Confirm', cancelLabel: 'Cancel', danger: false },
)
const emit = defineEmits<{ confirm: []; cancel: [] }>()
const cancelBtn = ref<HTMLButtonElement | null>(null)

watch(() => props.open, async (o) => {
  if (o) {
    await nextTick()
    cancelBtn.value?.focus()
  }
})
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="overlay" @click.self="emit('cancel')" @keydown.esc="emit('cancel')">
      <div class="dialog" role="alertdialog" aria-modal="true" aria-labelledby="cd-title" :aria-describedby="message ? 'cd-msg' : undefined">
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
