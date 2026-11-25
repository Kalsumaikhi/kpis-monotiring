<script setup lang="ts">
import { toasts, dismissToast } from '../composables/useToast'
</script>

<template>
  <div class="toast-host" aria-live="polite" aria-atomic="false">
    <div v-for="t in toasts" :key="t.id" class="toast" :class="t.tone" role="status">
      <span>{{ t.message }}</span>
      <button type="button" class="x" aria-label="Dismiss" @click="dismissToast(t.id)">×</button>
    </div>
  </div>
</template>

<style scoped>
.toast-host { position: fixed; z-index: 60; right: 16px; bottom: 16px; left: 16px; display: flex; flex-direction: column; align-items: flex-end; gap: 8px; pointer-events: none; }
.toast {
  pointer-events: auto; display: flex; align-items: center; gap: 12px; max-width: 420px;
  padding: 10px 14px; border-radius: var(--radius); background: var(--surface); color: var(--text);
  border: 1px solid var(--border); border-left: 4px solid var(--status-ok); box-shadow: var(--shadow); font-weight: 600;
}
.toast.error { border-left-color: var(--status-fail); }
.x { background: none; border: 0; color: var(--text-muted); font-size: 1.25rem; line-height: 1; cursor: pointer; min-width: 28px; min-height: 28px; }
</style>
