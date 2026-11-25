<script setup lang="ts">
defineProps<{ title: string; hint?: string }>()
</script>

<template>
  <div class="empty">
    <svg viewBox="0 0 48 32" width="48" height="32" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M4 26h40" /><path d="M8 20l9-8 8 5 9-10 6 6" stroke-dasharray="2 4" />
    </svg>
    <p class="title">{{ title }}</p>
    <p v-if="hint" class="muted">{{ hint }}</p>
    <slot />
  </div>
</template>

<style scoped>
.empty { display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 32px 16px; text-align: center; color: var(--text-muted); }
.title { font-weight: 600; color: var(--text); }
</style>
