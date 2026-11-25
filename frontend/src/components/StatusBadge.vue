<script setup lang="ts">
import { computed } from 'vue'

type Tone = 'ok' | 'warn' | 'fail' | 'muted'
type Icon = 'check' | 'dash' | 'cross' | 'alert' | 'clock' | 'spinner' | 'circle' | 'sync' | 'ban'

const MAP: Record<string, { label: string; tone: Tone; icon: Icon }> = {
  ok: { label: 'OK', tone: 'ok', icon: 'check' },
  baseline: { label: 'Baseline', tone: 'ok', icon: 'circle' },
  no_change: { label: 'No change', tone: 'muted', icon: 'dash' },
  failed: { label: 'Failed', tone: 'fail', icon: 'cross' },
  suspect_pending: { label: 'Needs review', tone: 'warn', icon: 'alert' },
  approve_queued: { label: 'Applying…', tone: 'warn', icon: 'sync' },
  rejected: { label: 'Rejected', tone: 'muted', icon: 'ban' },
  superseded: { label: 'Superseded', tone: 'muted', icon: 'dash' },
  running: { label: 'Running', tone: 'muted', icon: 'spinner' },
  queued: { label: 'Queued', tone: 'muted', icon: 'clock' },
  blocked: { label: 'Blocked', tone: 'warn', icon: 'ban' },
  never: { label: 'Not checked yet', tone: 'muted', icon: 'clock' },
}

const props = defineProps<{ status: string }>()
const info = computed(() => MAP[props.status] ?? { label: props.status, tone: 'muted' as Tone, icon: 'circle' as Icon })
</script>

<template>
  <span class="badge" :class="`tone-${info.tone}`">
    <svg class="icon" :class="{ spin: info.icon === 'spinner' || info.icon === 'sync' }" data-icon :data-icon-name="info.icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
      <path v-if="info.icon === 'check'" d="M3 8.5l3.2 3.2L13 4.8" />
      <circle v-else-if="info.icon === 'circle'" cx="8" cy="8" r="4.5" />
      <path v-else-if="info.icon === 'dash'" d="M4 8h8" />
      <path v-else-if="info.icon === 'cross'" d="M4.5 4.5l7 7M11.5 4.5l-7 7" />
      <path v-else-if="info.icon === 'alert'" d="M8 3v6M8 12v.01" />
      <path v-else-if="info.icon === 'clock'" d="M8 4.5V8l2.5 1.5M8 14a6 6 0 100-12 6 6 0 000 12z" />
      <path v-else-if="info.icon === 'spinner'" d="M8 2a6 6 0 106 6" />
      <path v-else-if="info.icon === 'sync'" d="M13 8a5 5 0 01-9 3M3 8a5 5 0 019-3M12 2v3h-3M4 14v-3h3" />
      <path v-else d="M8 14a6 6 0 100-12 6 6 0 000 12zM3.8 3.8l8.4 8.4" />
    </svg>
    <span>{{ info.label }}</span>
  </span>
</template>

<style scoped>
.badge {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 1px 8px 1px 6px; border-radius: 999px;
  border: 1px solid currentColor; font-size: 0.8125rem; font-weight: 600; line-height: 1.5;
  white-space: nowrap; background: transparent;
}
.tone-ok { color: var(--status-ok); }
.tone-warn { color: var(--status-warn); }
.tone-fail { color: var(--status-fail); }
.tone-muted { color: var(--text-muted); }
.spin { animation: spin 1s linear infinite; transform-origin: center; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
