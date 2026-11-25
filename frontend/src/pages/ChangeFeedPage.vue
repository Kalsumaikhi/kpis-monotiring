<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ApiError, apiGet } from '../api/client'
import type { Paged, Run, Source } from '../api/types'
import DataTable, { type Column } from '../components/DataTable.vue'
import Pager from '../components/Pager.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { absoluteTime, fmtNum, relativeTime } from '../lib/format'

const PAGE_SIZE = 50
const STATUSES = ['ok', 'baseline', 'no_change', 'failed', 'suspect_pending', 'approve_queued', 'rejected', 'superseded', 'blocked', 'running', 'queued']

const route = useRoute()
const router = useRouter()

const sources = ref<Source[]>([])
const rows = ref<Run[]>([])
const total = ref(0)
const loading = ref(true)
const error = ref<string | null>(null)

const str = (v: unknown) => (typeof v === 'string' ? v : '')
const filters = computed(() => ({
  source: str(route.query.source),
  status: str(route.query.status),
  since: str(route.query.since),
  onlyChanges: str(route.query.changes) !== '0',
  page: Math.max(1, Number(str(route.query.page)) || 1),
}))

const columns: Column[] = [
  { key: 'started_at', label: 'Time' },
  { key: 'source', label: 'Source' },
  { key: 'status', label: 'Status' },
  { key: 'added', label: 'Added', align: 'right' },
  { key: 'altered', label: 'Altered', align: 'right' },
  { key: 'removed', label: 'Removed', align: 'right' },
  { key: 'vintage', label: 'Vintage' },
]

function setQuery(patch: Record<string, string | undefined>) {
  const q: Record<string, string> = {}
  for (const [k, v] of Object.entries({ ...route.query, ...patch })) {
    if (typeof v === 'string' && v !== '') q[k] = v
  }
  router.replace({ query: q })
}
const setFilter = (patch: Record<string, string | undefined>) => setQuery({ ...patch, page: undefined })

let seq = 0
async function load() {
  const mine = ++seq
  const f = filters.value
  loading.value = true
  error.value = null
  try {
    const res = await apiGet<Paged<Run>>('/api/runs', {
      source: f.source,
      status: f.status,
      since: f.since ? new Date(`${f.since}T00:00:00`).toISOString() : undefined,
      has_changes: f.onlyChanges ? true : undefined,
      page: f.page,
      page_size: PAGE_SIZE,
    })
    if (mine !== seq) return
    rows.value = res.items
    total.value = res.total
  } catch (e) {
    if (mine !== seq) return
    error.value = e instanceof ApiError ? e.detail : 'Could not load runs'
  } finally {
    if (mine === seq) loading.value = false
  }
}

watch(() => route.query, () => { if (route.name === 'changes' || route.path === '/changes') load() })
onMounted(async () => {
  load()
  try {
    sources.value = await apiGet<Source[]>('/api/sources')
  } catch {
    /* the source filter simply stays empty */
  }
})

const signed = (r: Run) => ({ added: `+${fmtNum(r.added)}`, altered: `~${fmtNum(r.altered)}`, removed: `−${fmtNum(r.removed)}` })
function open(r: Run) { router.push(`/runs/${r.id}`) }
</script>

<template>
  <div class="page">
    <div class="page-head"><h1>Changes</h1></div>

    <form class="filters card" @submit.prevent>
      <div class="field">
        <label for="f-source">Source</label>
        <select id="f-source" :value="filters.source" @change="setFilter({ source: ($event.target as HTMLSelectElement).value })">
          <option value="">All sources</option>
          <option v-for="s in sources" :key="s.slug" :value="s.slug">{{ s.name }}</option>
        </select>
      </div>
      <div class="field">
        <label for="f-status">Status</label>
        <select id="f-status" :value="filters.status" @change="setFilter({ status: ($event.target as HTMLSelectElement).value })">
          <option value="">Any status</option>
          <option v-for="s in STATUSES" :key="s" :value="s">{{ s.replace('_', ' ') }}</option>
        </select>
      </div>
      <div class="field">
        <label for="f-since">Since</label>
        <input id="f-since" type="date" :value="filters.since" @change="setFilter({ since: ($event.target as HTMLInputElement).value })" />
      </div>
      <label class="check">
        <input type="checkbox" :checked="filters.onlyChanges" @change="setFilter({ changes: ($event.target as HTMLInputElement).checked ? undefined : '0' })" />
        Only runs with changes
      </label>
    </form>

    <div v-if="error" class="toolbar">
      <p class="error-text" role="alert">{{ error }}</p>
      <button type="button" class="btn secondary" @click="load">Retry</button>
    </div>

    <DataTable :columns="columns" :rows="rows" :loading="loading" row-key="id" class="feed" @row-click="open"
      :empty="filters.onlyChanges ? 'No runs with changes match these filters.' : 'No runs match these filters.'">
      <template #cell-started_at="{ row }">
        <RouterLink :to="`/runs/${row.id}`" :title="absoluteTime(row.started_at)" @click.stop>{{ relativeTime(row.started_at) }}</RouterLink>
      </template>
      <template #cell-status="{ row }"><StatusBadge :status="row.status" /></template>
      <template #cell-added="{ row }"><span class="added">{{ signed(row).added }}</span></template>
      <template #cell-altered="{ row }"><span class="altered">{{ signed(row).altered }}</span></template>
      <template #cell-removed="{ row }"><span class="removed">{{ signed(row).removed }}</span></template>
      <template #cell-vintage="{ row }">{{ row.vintage ?? '—' }}</template>
    </DataTable>
    <Pager :page="filters.page" :page-size="PAGE_SIZE" :total="total" @update:page="(p) => setQuery({ page: p === 1 ? undefined : String(p) })" />
  </div>
</template>

<style scoped>
.filters { display: flex; flex-wrap: wrap; gap: 12px 16px; align-items: flex-end; }
.filters .field { min-width: 0; }
.check { display: inline-flex; align-items: center; gap: 8px; min-height: 38px; font-weight: 500; }
.added { color: var(--chg-added); font-weight: 600; }
.altered { color: var(--chg-altered); font-weight: 600; }
.removed { color: var(--chg-removed); font-weight: 600; }
</style>

<style>
.feed tbody tr { cursor: pointer; }
</style>
