<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ApiError, apiGet } from '../api/client'
import type { KpiRow, KpiSort, Paged, Source } from '../api/types'
import DataTable, { type Column } from '../components/DataTable.vue'
import Pager from '../components/Pager.vue'
import Sparkline from '../components/Sparkline.vue'
import { useQuerySearch } from '../composables/useQuerySearch'
import { fmtInterval, fmtNum } from '../lib/format'

const PAGE_SIZE = 50
const SORTS: KpiSort[] = ['changes_30d', 'changes_12m', 'last_changed', 'typical_interval']
const route = useRoute()
const router = useRouter()
const search = useQuerySearch()

const sources = ref<Source[]>([])
const rows = ref<KpiRow[]>([])
const total = ref(0)
const loading = ref(true)
const error = ref<string | null>(null)

const str = (v: unknown) => (typeof v === 'string' ? v : '')
const query = computed(() => {
  const s = str(route.query.sort) as KpiSort
  return {
    source: str(route.query.source),
    q: str(route.query.q),
    sort: SORTS.includes(s) ? s : 'changes_30d',
    order: str(route.query.order) === 'asc' ? ('asc' as const) : ('desc' as const),
    page: Math.max(1, Number(str(route.query.page)) || 1),
  }
})

const columns: Column[] = [
  { key: 'name', label: 'Indicator' },
  { key: 'source', label: 'Source' },
  { key: 'changes_30d', label: '30 days', align: 'right', sortKey: 'changes_30d' },
  { key: 'changes_12m', label: '12 months', align: 'right', sortKey: 'changes_12m' },
  { key: 'changes_all', label: 'All time', align: 'right' },
  { key: 'last_changed', label: 'Last changed', sortKey: 'last_changed' },
  { key: 'typical_interval_days', label: 'Typical interval', sortKey: 'typical_interval' },
  { key: 'sparkline', label: 'Last 12 months' },
]

function setQuery(patch: Record<string, string | undefined>) {
  const q: Record<string, string> = {}
  for (const [k, v] of Object.entries({ ...route.query, ...patch })) if (typeof v === 'string' && v !== '') q[k] = v
  router.replace({ query: q })
}
function onSort(key: string) {
  const cur = query.value
  const order = cur.sort === key ? (cur.order === 'desc' ? 'asc' : 'desc') : 'desc'
  setQuery({ sort: key, order, page: undefined })
}

let seq = 0
async function load() {
  const mine = ++seq
  const f = query.value
  loading.value = true
  error.value = null
  try {
    const res = await apiGet<Paged<KpiRow>>('/api/kpis', {
      source: f.source || undefined, q: f.q || undefined, sort: f.sort, order: f.order, page: f.page, page_size: PAGE_SIZE,
    })
    if (mine !== seq) return
    rows.value = res.items
    total.value = res.total
  } catch (e) {
    if (mine !== seq) return
    error.value = e instanceof ApiError ? e.detail : 'Could not load KPIs'
  } finally {
    if (mine === seq) loading.value = false
  }
}

watch(() => route.query, () => { if (route.name === 'kpis') load() })
onMounted(async () => {
  load()
  try {
    sources.value = await apiGet<Source[]>('/api/sources')
  } catch {
    /* the source filter stays empty */
  }
})

const rowId = (r: KpiRow) => `${r.source}/${r.indicator}`
const keyed = computed(() => rows.value.map((r) => ({ ...r, id: rowId(r) })))
const open = (r: KpiRow) => router.push({ name: 'explorer', params: { slug: r.source, indicator: r.indicator } })
</script>

<template>
  <div class="page">
    <div class="page-head"><h1>KPI frequency</h1><p class="muted">How often each indicator changes.</p></div>

    <form class="filters card" @submit.prevent>
      <div class="field">
        <label for="k-source">Source</label>
        <select id="k-source" :value="query.source" @change="setQuery({ source: ($event.target as HTMLSelectElement).value, page: undefined })">
          <option value="">All sources</option>
          <option v-for="s in sources" :key="s.slug" :value="s.slug">{{ s.name }}</option>
        </select>
      </div>
      <div class="field grow">
        <label for="k-q">Search indicators</label>
        <input id="k-q" v-model="search" type="search" placeholder="Name or code" autocomplete="off" />
      </div>
    </form>

    <div v-if="error" class="toolbar">
      <p class="error-text" role="alert">{{ error }}</p>
      <button type="button" class="btn secondary" @click="load">Retry</button>
    </div>

    <DataTable :columns="columns" :rows="keyed" :loading="loading" row-key="id" class="kpis" :sort="query.sort" :order="query.order"
      empty="No indicators match these filters." @sort="onSort" @row-click="open">
      <template #cell-name="{ row }">
        <RouterLink :to="{ name: 'explorer', params: { slug: row.source, indicator: row.indicator } }" @click.stop>{{ row.name }}</RouterLink>
        <span v-if="row.name !== row.indicator" class="muted code"> {{ row.indicator }}</span>
      </template>
      <template #cell-changes_30d="{ row }">{{ fmtNum(row.changes_30d) }}</template>
      <template #cell-changes_12m="{ row }">{{ fmtNum(row.changes_12m) }}</template>
      <template #cell-changes_all="{ row }">{{ fmtNum(row.changes_all) }}</template>
      <template #cell-last_changed="{ row }">{{ row.last_changed ?? '—' }}</template>
      <template #cell-typical_interval_days="{ row }">{{ fmtInterval(row.typical_interval_days) }}</template>
      <template #cell-sparkline="{ row }"><Sparkline :values="row.sparkline" /></template>
    </DataTable>
    <Pager :page="query.page" :page-size="PAGE_SIZE" :total="total" @update:page="(p) => setQuery({ page: p === 1 ? undefined : String(p) })" />
  </div>
</template>

<style scoped>
.filters { display: flex; flex-wrap: wrap; gap: 12px 16px; align-items: flex-end; }
.filters .grow { flex: 1 1 220px; min-width: 0; }
.code { font-family: var(--font-mono); font-size: 0.85em; margin-left: 6px; }
</style>

<style>
.kpis tbody tr { cursor: pointer; }
</style>
