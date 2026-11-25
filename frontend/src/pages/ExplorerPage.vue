<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ApiError, apiGet } from '../api/client'
import type { ChangeType, CountryRow, IndicatorRow, Paged, Series, Source } from '../api/types'
import Attribution from '../components/Attribution.vue'
import DataTable, { type Column } from '../components/DataTable.vue'
import EmptyState from '../components/EmptyState.vue'
import Pager from '../components/Pager.vue'
import { useQuerySearch } from '../composables/useQuerySearch'
import { absoluteTime, fmtNum, fmtValue, relativeTime } from '../lib/format'
import { dimsLabel, groupSeries, isNumericSeries, MAX_SERIES, sortPeriods } from '../lib/series'

import SeriesChart from '../components/SeriesChart.vue'

const PAGE_SIZE = 50
const route = useRoute()
const router = useRouter()
const search = useQuerySearch()

const slug = computed(() => (typeof route.params.slug === 'string' ? route.params.slug : ''))
const indicator = computed(() => (typeof route.params.indicator === 'string' ? route.params.indicator : ''))
const countryParam = computed(() => (typeof route.params.country === 'string' ? route.params.country : ''))
const page = computed(() => Math.max(1, Number(route.query.page) || 1))
const q = computed(() => (typeof route.query.q === 'string' ? route.query.q : ''))

const sources = ref<Source[]>([])
const indicators = ref<IndicatorRow[]>([])
const countries = ref<CountryRow[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref<string | null>(null)
const series = ref<Series | null>(null)
// Watch sources have no countries: a lone "" entry means we go straight to the series for country "".
const noCountry = ref(false)

const sourceName = computed(() => sources.value.find((s) => s.slug === slug.value)?.name ?? slug.value)
const indicatorName = computed(() => indicators.value.find((i) => i.code === indicator.value)?.name || indicator.value)
const step = computed<1 | 2 | 3 | 4>(() => {
  if (!slug.value) return 1
  if (!indicator.value) return 2
  return countryParam.value || noCountry.value ? 4 : 3
})
const country = computed(() => countryParam.value)

let seq = 0
async function guarded<T>(fn: () => Promise<T>, apply: (v: T) => void, fallback: string) {
  const mine = ++seq
  loading.value = true
  error.value = null
  try {
    const v = await fn()
    if (mine !== seq) return
    apply(v)
  } catch (e) {
    if (mine !== seq) return
    error.value = e instanceof ApiError ? e.detail : fallback
  } finally {
    if (mine === seq) loading.value = false
  }
}

async function loadSources() {
  try {
    sources.value = await apiGet<Source[]>('/api/sources')
  } catch (e) {
    error.value = e instanceof ApiError ? e.detail : 'Could not load sources'
  }
}

function loadSeries(c: string) {
  series.value = null
  return guarded(
    () => apiGet<Series>(`/api/sources/${encodeURIComponent(slug.value)}/series`, { indicator: indicator.value, country: c }),
    (v) => { series.value = v; selected.value = groups.value.slice(0, MAX_SERIES).map((g) => g.key); pointPage.value = 1; historyPage.value = 1 },
    'Could not load the series',
  )
}

function load() {
  if (route.name !== 'explorer') return
  const s = encodeURIComponent(slug.value)
  const params = { q: q.value || undefined, page: page.value, page_size: PAGE_SIZE }
  if (step.value === 1) return loadSources().then(() => undefined)
  if (!sources.value.length) loadSources()
  if (step.value === 2) {
    return guarded(() => apiGet<Paged<IndicatorRow>>(`/api/sources/${s}/indicators`, params), (v) => { indicators.value = v.items; total.value = v.total }, 'Could not load indicators')
  }
  if (!indicator.value) return
  if (countryParam.value || noCountry.value) return loadSeries(countryParam.value)
  return guarded(
    () => apiGet<Paged<CountryRow>>(`/api/sources/${s}/indicators/${encodeURIComponent(indicator.value)}/countries`, params),
    (v) => {
      countries.value = v.items
      total.value = v.total
      if (!q.value && v.total === 1 && v.items[0]?.country === '') {
        noCountry.value = true
        loadSeries('')
      }
    },
    'Could not load countries',
  )
}

watch(() => [route.name, slug.value, indicator.value], () => { noCountry.value = false; series.value = null })
watch(
  () => [route.name, slug.value, indicator.value, countryParam.value, q.value, page.value],
  load,
  { immediate: true },
)

const go = (params: Record<string, string>) => router.push({ name: 'explorer', params })
const setPage = (p: number) => router.replace({ query: { ...route.query, page: p === 1 ? undefined : String(p) } })

const indicatorCols: Column[] = [
  { key: 'name', label: 'Indicator' },
  { key: 'unit', label: 'Unit' },
  { key: 'points', label: 'Data points', align: 'right' },
]
const countryCols: Column[] = [
  { key: 'country', label: 'Country' },
  { key: 'points', label: 'Data points', align: 'right' },
  { key: 'last_changed', label: 'Last changed' },
]

// ---- series step ----
const points = computed(() => series.value?.points ?? [])
const groups = computed(() => groupSeries(points.value))
const numeric = computed(() => isNumericSeries(points.value))
const selected = ref<string[]>([])
const hasDims = computed(() => points.value.some((p) => dimsLabel(p.dims)))
const chartLabels = computed(() => sortPeriods(points.value.map((p) => p.period)))
const chartSeries = computed(() =>
  groups.value
    .map((g, i) => ({ g, i }))
    .filter(({ g }) => selected.value.includes(g.key))
    .slice(0, MAX_SERIES)
    .map(({ g, i }) => ({
      label: g.label,
      colorIndex: i % MAX_SERIES,
      data: chartLabels.value.map((p) => {
        const v = g.values.get(p)
        return v === null || v === undefined ? null : Number(v)
      }),
    })),
)
function onPickSeries(ev: Event) {
  const picked = [...(ev.target as HTMLSelectElement).selectedOptions].map((o) => o.value)
  selected.value = picked.slice(0, MAX_SERIES)
}
const chartAria = computed(() => `Line chart of ${indicatorName.value}${country.value ? ` for ${country.value}` : ''}: ${chartSeries.value.length} series over ${chartLabels.value.length} periods. The table below has the same values.`)

const pointPage = ref(1)
const historyPage = ref(1)
const pointRows = computed(() => points.value.map((p, i) => ({ ...p, id: i, dimsText: dimsLabel(p.dims) || '—' })))
const pointCols = computed<Column[]>(() => [
  { key: 'period', label: 'Period' },
  ...(hasDims.value ? [{ key: 'dimsText', label: 'Dimensions' }] : []),
  { key: 'value', label: 'Value', align: 'right' as const },
])
const pagedPoints = computed(() => pointRows.value.slice((pointPage.value - 1) * PAGE_SIZE, pointPage.value * PAGE_SIZE))
const history = computed(() => (series.value?.history ?? []).map((h, i) => ({ ...h, id: i })))
const pagedHistory = computed(() => history.value.slice((historyPage.value - 1) * PAGE_SIZE, historyPage.value * PAGE_SIZE))
const historyCols: Column[] = [
  { key: 'created_at', label: 'When' },
  { key: 'change_type', label: 'Type' },
  { key: 'period', label: 'Period' },
  { key: 'change', label: 'Old → new' },
  { key: 'run_id', label: 'Run' },
]
const SIGN: Record<ChangeType, string> = { added: '+', altered: '~', removed: '−' }
</script>

<template>
  <div class="page">
    <div class="page-head"><h1>Explorer</h1></div>

    <nav class="crumbs" aria-label="Explorer path">
      <RouterLink v-if="step > 1" :to="{ name: 'explorer', params: {} }">All sources</RouterLink>
      <span v-else aria-current="page">All sources</span>
      <template v-if="step > 1">
        <span aria-hidden="true">/</span>
        <RouterLink v-if="step > 2" :to="{ name: 'explorer', params: { slug } }">{{ sourceName }}</RouterLink>
        <span v-else aria-current="page">{{ sourceName }}</span>
      </template>
      <template v-if="step > 2">
        <span aria-hidden="true">/</span>
        <RouterLink v-if="step > 3 && countryParam" :to="{ name: 'explorer', params: { slug, indicator } }">{{ indicatorName }}</RouterLink>
        <span v-else :aria-current="countryParam ? undefined : 'page'">{{ indicatorName }}</span>
      </template>
      <template v-if="step > 3 && countryParam">
        <span aria-hidden="true">/</span>
        <span aria-current="page">{{ countryParam }}</span>
      </template>
    </nav>

    <p v-if="error" class="error-text" role="alert">{{ error }}</p>

    <!-- Step 1: source -->
    <template v-if="step === 1">
      <EmptyState v-if="!sources.length && !error" title="No sources yet" hint="Sources appear here once they are configured." />
      <ul v-else class="grid">
        <li v-for="s in sources" :key="s.slug" class="card">
          <h2><RouterLink data-test="source-link" :to="{ name: 'explorer', params: { slug: s.slug } }">{{ s.name }}</RouterLink></h2>
          <p class="muted">{{ s.area }} · {{ fmtNum(s.rows_tracked) }} rows tracked</p>
        </li>
      </ul>
    </template>

    <!-- Step 2: indicator -->
    <template v-else-if="step === 2">
      <div class="field search">
        <label for="ex-q">Search indicators</label>
        <input id="ex-q" v-model="search" type="search" placeholder="Name or code" autocomplete="off" />
      </div>
      <DataTable :columns="indicatorCols" :rows="indicators" :loading="loading" row-key="code" class="pick"
        :empty="q ? 'No indicators match your search.' : 'No indicators yet.'"
        @row-click="(r) => go({ slug, indicator: r.code })">
        <template #cell-name="{ row }">
          <RouterLink :to="{ name: 'explorer', params: { slug, indicator: row.code } }" @click.stop>{{ row.name || row.code }}</RouterLink>
          <span v-if="row.name && row.name !== row.code" class="muted code"> {{ row.code }}</span>
        </template>
        <template #cell-unit="{ row }">{{ row.unit ?? '—' }}</template>
        <template #cell-points="{ row }">{{ fmtNum(row.points) }}</template>
      </DataTable>
      <Pager :page="page" :page-size="PAGE_SIZE" :total="total" @update:page="setPage" />
    </template>

    <!-- Step 3: country -->
    <template v-else-if="step === 3">
      <div class="field search">
        <label for="ex-q">Search countries</label>
        <input id="ex-q" v-model="search" type="search" placeholder="Country" autocomplete="off" />
      </div>
      <DataTable :columns="countryCols" :rows="countries" :loading="loading" row-key="country" class="pick"
        :empty="q ? 'No countries match your search.' : 'No countries for this indicator.'"
        @row-click="(r) => go({ slug, indicator, country: r.country })">
        <template #cell-country="{ row }">
          <RouterLink :to="{ name: 'explorer', params: { slug, indicator, country: row.country } }" @click.stop>{{ row.country }}</RouterLink>
        </template>
        <template #cell-points="{ row }">{{ fmtNum(row.points) }}</template>
        <template #cell-last_changed="{ row }"><span :title="absoluteTime(row.last_changed)">{{ relativeTime(row.last_changed) }}</span></template>
      </DataTable>
      <Pager :page="page" :page-size="PAGE_SIZE" :total="total" @update:page="setPage" />
    </template>

    <!-- Step 4: series -->
    <template v-else>
      <p v-if="loading && !series" class="muted">Loading series…</p>
      <template v-else-if="series">
        <EmptyState v-if="!points.length" title="No data points" hint="Nothing has been recorded for this selection yet." />
        <template v-else>
          <section class="card" aria-labelledby="ex-chart-h">
            <h2 id="ex-chart-h">{{ indicatorName }}<span v-if="countryParam" class="muted"> · {{ countryParam }}</span></h2>
            <template v-if="numeric">
              <div v-if="groups.length > MAX_SERIES" class="series-pick" data-test="series-note">
                <p class="muted">Showing {{ Math.min(selected.length, MAX_SERIES) }} of {{ groups.length }} series</p>
                <label for="ex-series">Choose series (up to {{ MAX_SERIES }})</label>
                <select id="ex-series" multiple size="6" @change="onPickSeries">
                  <option v-for="g in groups" :key="g.key" :value="g.key" :selected="selected.includes(g.key)">{{ g.label }}</option>
                </select>
              </div>
              <SeriesChart :labels="chartLabels" :series="chartSeries" :label="chartAria" data-test="chart" />
            </template>
            <p v-else class="muted" data-test="text-only">These values are not numeric, so they are shown as a table only.</p>
          </section>

          <section class="stack" aria-labelledby="ex-points-h">
            <h2 id="ex-points-h">Data points</h2>
            <DataTable :columns="pointCols" :rows="pagedPoints" row-key="id">
              <template #cell-value="{ row }">{{ fmtValue(row.value) }}</template>
            </DataTable>
            <Pager v-if="pointRows.length > PAGE_SIZE" :page="pointPage" :page-size="PAGE_SIZE" :total="pointRows.length" @update:page="(p) => (pointPage = p)" />
          </section>

          <section class="stack" aria-labelledby="ex-hist-h">
            <h2 id="ex-hist-h">Change history</h2>
            <DataTable :columns="historyCols" :rows="pagedHistory" row-key="id" empty="No changes recorded yet.">
              <template #cell-created_at="{ row }"><span :title="absoluteTime(row.created_at)">{{ relativeTime(row.created_at) }}</span></template>
              <template #cell-change_type="{ row }"><span :class="`chg ${row.change_type}`"><span aria-hidden="true">{{ SIGN[row.change_type as ChangeType] }}</span> {{ row.change_type }}</span></template>
              <template #cell-period="{ row }">{{ row.period }}<span v-if="dimsLabel(row.dims)" class="muted"> ({{ dimsLabel(row.dims) }})</span></template>
              <template #cell-change="{ row }">{{ fmtValue(row.old) }} → {{ fmtValue(row.new) }}</template>
              <template #cell-run_id="{ row }"><RouterLink :to="`/runs/${row.run_id}`">Run #{{ row.run_id }}</RouterLink></template>
            </DataTable>
            <Pager v-if="history.length > PAGE_SIZE" :page="historyPage" :page-size="PAGE_SIZE" :total="history.length" @update:page="(p) => (historyPage = p)" />
          </section>
          <Attribution :text="series.attribution" />
        </template>
      </template>
    </template>
  </div>
</template>

<style scoped>
.crumbs { display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: center; font-size: 0.9rem; }
.search { max-width: 360px; }
.grid { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--gap); grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); }
.stack { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.series-pick { display: flex; flex-direction: column; gap: 6px; margin: 8px 0; max-width: 420px; }
.series-pick select { min-height: 0; }
.code { font-family: var(--font-mono); font-size: 0.85em; margin-left: 6px; }
.chg { font-weight: 600; }
.chg.added { color: var(--chg-added); }
.chg.altered { color: var(--chg-altered); }
.chg.removed { color: var(--chg-removed); }
.card h2 { margin-bottom: 10px; }
</style>

<style>
.pick tbody tr { cursor: pointer; }
</style>
