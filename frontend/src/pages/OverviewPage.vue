<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { ApiError, apiGet, apiSend } from '../api/client'
import type { Source } from '../api/types'
import StatusBadge from '../components/StatusBadge.vue'
import EmptyState from '../components/EmptyState.vue'
import { useToast } from '../composables/useToast'

const sources = ref<Source[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const running = ref<Set<string>>(new Set())
const { show } = useToast()

const nf = new Intl.NumberFormat('en-US')
const fmt = (n: number) => nf.format(n)

function needsAttention(s: Source): boolean {
  const st = s.last_run?.status
  return st === 'failed' || st === 'suspect_pending' || st === 'approve_queued' ||
    s.failing_streak > 0 || s.incomplete || s.noisy_targets > 0
}

const sorted = computed(() => {
  const withIdx = sources.value.map((s, i) => ({ s, i, a: needsAttention(s) }))
  withIdx.sort((x, y) => Number(y.a) - Number(x.a) || x.i - y.i)
  return withIdx.map((x) => x.s)
})
const attentionCount = computed(() => sources.value.filter(needsAttention).length)
const changes30 = computed(() => sources.value.reduce((n, s) => n + s.changes_30d, 0))

function relative(iso: string | null): string {
  if (!iso) return 'never'
  const diff = (new Date(iso).getTime() - Date.now()) / 1000
  const units: [Intl.RelativeTimeFormatUnit, number][] = [['day', 86400], ['hour', 3600], ['minute', 60]]
  const rtf = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })
  for (const [u, s] of units) if (Math.abs(diff) >= s) return rtf.format(Math.round(diff / s), u)
  return 'just now'
}
const IN_FLIGHT: Record<string, string> = { running: 'In progress', queued: 'Queued', approve_queued: 'Applying…', suspect_pending: 'Needs review' }
function checkLabel(r: NonNullable<Source['last_run']>): string {
  return r.finished_at ? relative(r.finished_at) : (IN_FLIGHT[r.status] ?? 'not finished')
}
const absolute = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : '')

async function load() {
  loading.value = true
  error.value = null
  try {
    sources.value = await apiGet<Source[]>('/api/sources')
  } catch (e) {
    error.value = e instanceof ApiError ? e.detail : 'Could not load sources'
  } finally {
    loading.value = false
  }
}

async function runNow(s: Source) {
  if (running.value.has(s.slug)) return
  running.value = new Set(running.value).add(s.slug)
  try {
    await apiSend('POST', `/api/sources/${s.slug}/run`)
    show('Check queued')
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) show('Already running')
    else show(`Couldn't start check: ${e instanceof ApiError ? e.detail : 'unknown error'}`, 'error')
  } finally {
    const next = new Set(running.value)
    next.delete(s.slug)
    running.value = next
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-head"><h1>Overview</h1></div>

    <div v-if="error" class="toolbar">
      <p class="error-text" role="alert">{{ error }}</p>
      <button type="button" class="btn secondary" @click="load">Retry</button>
    </div>
    <p v-else-if="loading" class="muted">Loading sources…</p>

    <EmptyState v-else-if="!sources.length" title="No sources yet" hint="Add a custom source in Settings to start tracking.">
      <RouterLink class="btn" to="/settings">Go to Settings</RouterLink>
    </EmptyState>

    <template v-else>
      <section class="stats" aria-label="Summary">
        <div class="tile" data-test="stat-tracked">
          <span class="num">{{ fmt(sources.length) }}</span><span class="lbl">Sources tracked</span>
        </div>
        <div class="tile" :class="{ alert: attentionCount > 0 }" data-test="stat-attention">
          <span class="num">{{ fmt(attentionCount) }}</span><span class="lbl">Need attention</span>
        </div>
        <div class="tile" data-test="stat-changes">
          <span class="num">{{ fmt(changes30) }}</span><span class="lbl">Changes in 30 days</span>
        </div>
      </section>

      <ul class="grid">
        <li v-for="s in sorted" :key="s.slug" class="card source" :class="{ attn: needsAttention(s) }">
          <header>
            <div class="title">
              <h2><RouterLink data-test="source-name" :to="`/explorer/${s.slug}`">{{ s.name }}</RouterLink></h2>
              <span class="muted area">{{ s.area }}</span>
            </div>
            <StatusBadge v-if="s.last_run" :status="s.last_run.status" />
            <StatusBadge v-else status="never" />
          </header>

          <div v-if="s.incomplete || s.noisy_targets > 0 || s.failing_streak > 0" class="flags">
            <span v-if="s.failing_streak > 0" class="flag fail">Failing {{ s.failing_streak }}× in a row</span>
            <span v-if="s.incomplete" class="flag warn">Incomplete fetch</span>
            <RouterLink v-if="s.noisy_targets > 0" class="flag warn" to="/settings">
              {{ s.noisy_targets }} noisy {{ s.noisy_targets === 1 ? 'page' : 'pages' }}
            </RouterLink>
          </div>

          <dl>
            <div>
              <dt>Last success</dt>
              <dd :title="absolute(s.last_success_at)">{{ relative(s.last_success_at) }}</dd>
            </div>
            <div>
              <dt>Last check</dt>
              <dd>
                <RouterLink v-if="s.last_run" :to="`/runs/${s.last_run.id}`" :title="absolute(s.last_run.finished_at)">
                  {{ checkLabel(s.last_run) }}
                </RouterLink>
                <span v-else>never</span>
              </dd>
            </div>
            <div><dt>Last change</dt><dd :title="absolute(s.last_change_at)">{{ relative(s.last_change_at) }}</dd></div>
            <div><dt>Rows tracked</dt><dd>{{ fmt(s.rows_tracked) }}</dd></div>
            <div><dt>Changes (30 d)</dt><dd>{{ fmt(s.changes_30d) }}</dd></div>
          </dl>

          <footer>
            <p v-if="s.attribution" class="attr">{{ s.attribution }}</p>
            <button type="button" class="btn secondary run-now" :disabled="running.has(s.slug)" @click="runNow(s)">
              {{ running.has(s.slug) ? 'Starting…' : 'Run now' }}
            </button>
          </footer>
        </li>
      </ul>
    </template>
  </div>
</template>

<style scoped>
.stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px 16px; display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.tile .num { font-size: 1.75rem; font-weight: 650; line-height: 1.1; }
.tile .lbl { color: var(--text-muted); font-size: 0.8125rem; }
.tile.alert { border-left: 4px solid var(--status-warn); }
.grid { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 300px), 1fr)); gap: 12px; }
.source { display: flex; flex-direction: column; gap: 12px; }
.source.attn { border-left: 4px solid var(--status-warn); }
.source header { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; flex-wrap: wrap; }
.title { display: flex; flex-direction: column; min-width: 0; }
.title h2 { overflow-wrap: anywhere; }
.area { font-size: 0.8125rem; }
.flags { display: flex; flex-wrap: wrap; gap: 6px; }
.flag { font-size: 0.8125rem; font-weight: 600; padding: 1px 8px; border-radius: 999px; border: 1px solid currentColor; text-decoration: none; }
.flag.warn { color: var(--status-warn); }
.flag.fail { color: var(--status-fail); }
dl { margin: 0; display: grid; grid-template-columns: 1fr 1fr; gap: 8px 12px; }
dt { font-size: 0.75rem; color: var(--text-muted); }
dd { margin: 0; font-weight: 600; }
footer { margin-top: auto; display: flex; flex-direction: column; gap: 8px; align-items: flex-start; }
.attr { font-size: 0.75rem; color: var(--text-muted); }
@media (max-width: 480px) {
  .stats { gap: 8px; }
  .tile { padding: 10px; }
  .tile .num { font-size: 1.375rem; }
}
</style>
