<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { ApiError, apiGet, apiSend } from '../api/client'
import type { ChangeItem, ChangeType, RunChanges, RunDetail, RunStatusResult, Source } from '../api/types'
import Attribution from '../components/Attribution.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import DataTable, { type Column } from '../components/DataTable.vue'
import EmptyState from '../components/EmptyState.vue'
import Pager from '../components/Pager.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { useToast } from '../composables/useToast'
import { absoluteTime, fmtDims, fmtNum, fmtValue, relativeTime } from '../lib/format'

const PAGE_SIZE = 50
const POLL_MS = 5000
const POLL_MAX_MS = 10 * 60 * 1000

const route = useRoute()
const { show } = useToast()
const runId = computed(() => Number(route.params.id))

const run = ref<RunDetail | null>(null)
const attribution = ref<string | null>(null)
const loadError = ref<string | null>(null)
const notFound = ref(false)
const loading = ref(true)

const changes = ref<ChangeItem[]>([])
const changesTotal = ref(0)
const changesLoading = ref(false)
const changesError = ref<string | null>(null)
const page = ref(1)
const fType = ref<ChangeType | ''>('')
const fIndicator = ref('')
const fCountry = ref('')

const confirm = ref<'approve' | 'reject' | null>(null)
const busy = ref(false)
const pollNote = ref<string | null>(null)
let pollTimer: ReturnType<typeof setInterval> | null = null
let pollInFlight = false
let pollFailures = 0
let runSeq = 0
let changesSeq = 0

const isHeld = computed(() => run.value?.status === 'suspect_pending')
const isApplying = computed(() => run.value?.status === 'approve_queued')
const existing = computed(() => (run.value ? (run.value.rows_total ?? 0) - run.value.added + run.value.removed : 0))

const columns: Column[] = [
  { key: 'change_type', label: 'Type' },
  { key: 'indicator', label: 'Indicator' },
  { key: 'country', label: 'Country' },
  { key: 'period', label: 'Period' },
  { key: 'dims', label: 'Dims' },
  { key: 'values', label: 'Old → new' },
]
const SIGN: Record<ChangeType, string> = { added: '+', altered: '~', removed: '−' }

/** Returns true on success. With `keep`, a failure leaves the current run on screen. */
async function loadRun(keep = false): Promise<boolean> {
  const mine = ++runSeq
  try {
    const res = await apiGet<RunDetail>(`/api/runs/${runId.value}`)
    if (mine !== runSeq) return false
    run.value = res
    loadError.value = null
    notFound.value = false
    return true
  } catch (e) {
    if (mine !== runSeq) return false
    if (keep && run.value) return false
    run.value = null
    if (e instanceof ApiError && e.status === 404) notFound.value = true
    else loadError.value = e instanceof ApiError ? e.detail : 'Could not load this run'
    return false
  }
}

async function loadChanges(): Promise<void> {
  const mine = ++changesSeq
  changesLoading.value = true
  changesError.value = null
  try {
    const res = await apiGet<RunChanges>(`/api/runs/${runId.value}/changes`, {
      change_type: fType.value, indicator: fIndicator.value.trim(), country: fCountry.value.trim(),
      page: page.value, page_size: PAGE_SIZE,
    })
    if (mine !== changesSeq) return
    changes.value = res.items
    changesTotal.value = res.total
  } catch (e) {
    if (mine !== changesSeq) return
    changesError.value = e instanceof ApiError ? e.detail : 'Could not load changes'
  } finally {
    if (mine === changesSeq) changesLoading.value = false
  }
}

async function loadAll() {
  const id = runId.value
  loading.value = true
  await loadRun()
  if (id !== runId.value) return
  loading.value = false
  if (run.value) {
    loadChanges()
    loadAttribution()
    if (run.value.status === 'approve_queued') startPolling()
  }
}

async function loadAttribution() {
  const id = runId.value
  const slug = run.value?.source
  if (!slug) return
  try {
    const sources = await apiGet<Source[]>('/api/sources')
    if (id !== runId.value) return
    attribution.value = sources.find((s) => s.slug === slug)?.attribution ?? null
  } catch {
    attribution.value = null
  }
}

function stopPolling() {
  if (pollTimer !== null) clearInterval(pollTimer)
  pollTimer = null
  pollInFlight = false
}

function startPolling() {
  stopPolling()
  pollNote.value = null
  pollFailures = 0
  const id = runId.value
  const deadline = Date.now() + POLL_MAX_MS
  pollTimer = setInterval(async () => {
    if (pollInFlight) return
    if (Date.now() >= deadline) {
      stopPolling()
      pollNote.value = 'Still applying — refresh later'
      return
    }
    pollInFlight = true
    const ok = await loadRun(true)
    if (id !== runId.value || pollTimer === null) return
    pollInFlight = false
    if (!ok) {
      if (++pollFailures >= 3) {
        stopPolling()
        pollNote.value = 'Lost connection — refresh to check status'
      }
      return
    }
    pollFailures = 0
    if (run.value && run.value.status !== 'approve_queued') {
      stopPolling()
      page.value = 1
      loadChanges()
    }
  }, POLL_MS)
}

async function doApprove() {
  confirm.value = null
  busy.value = true
  const id = runId.value
  try {
    const res = await apiSend<RunStatusResult>('POST', `/api/runs/${id}/approve`)
    if (id !== runId.value) return
    if (run.value) run.value = { ...run.value, status: res.status }
    show('Approval queued')
    startPolling()
  } catch (e) {
    show(e instanceof ApiError ? e.detail : 'Could not approve', 'error')
    if (id === runId.value && e instanceof ApiError && e.status === 409) loadRun()
  } finally {
    if (id === runId.value) busy.value = false
  }
}

async function doReject() {
  confirm.value = null
  busy.value = true
  const id = runId.value
  try {
    await apiSend('POST', `/api/runs/${id}/reject`)
    show('Run rejected')
    if (id !== runId.value) return
    await loadRun()
    page.value = 1
    loadChanges()
  } catch (e) {
    show(e instanceof ApiError ? e.detail : 'Could not reject', 'error')
    if (id === runId.value && e instanceof ApiError && e.status === 409) loadRun()
  } finally {
    if (id === runId.value) busy.value = false
  }
}

function applyFilters() { page.value = 1; loadChanges() }
function setPage(p: number) { page.value = p; loadChanges() }

function resetState() {
  stopPolling()
  runSeq++
  changesSeq++
  run.value = null
  attribution.value = null
  loadError.value = null
  notFound.value = false
  changes.value = []
  changesTotal.value = 0
  changesError.value = null
  changesLoading.value = false
  pollNote.value = null
  pollFailures = 0
  confirm.value = null
  busy.value = false
  page.value = 1
  fType.value = ''
  fIndicator.value = ''
  fCountry.value = ''
}

watch(runId, (id) => { if (Number.isFinite(id)) { resetState(); loadAll() } })
onMounted(loadAll)
onBeforeUnmount(stopPolling)
</script>

<template>
  <div class="page">
    <p v-if="loading" class="muted">Loading run…</p>

    <EmptyState v-else-if="notFound" title="Run not found" hint="It may have been removed, or the link is wrong.">
      <RouterLink class="btn secondary" to="/changes">Back to changes</RouterLink>
    </EmptyState>

    <div v-else-if="loadError || !run" class="toolbar">
      <p class="error-text" role="alert">{{ loadError ?? 'Could not load this run' }}</p>
      <button type="button" class="btn secondary" @click="loadAll">Retry</button>
    </div>

    <template v-else>
      <div class="page-head">
        <h1>Run #{{ run.id }} <span class="muted src">· <RouterLink :to="`/explorer/${run.source}`">{{ run.source }}</RouterLink></span></h1>
        <StatusBadge :status="run.status" />
      </div>

      <section class="card" aria-label="Run summary">
        <dl class="meta">
          <div><dt>Started</dt><dd :title="relativeTime(run.started_at)">{{ run.started_at ? absoluteTime(run.started_at) : '—' }}</dd></div>
          <div><dt>Finished</dt><dd :title="relativeTime(run.finished_at)">{{ run.finished_at ? absoluteTime(run.finished_at) : '—' }}</dd></div>
          <div><dt>Trigger</dt><dd>{{ run.trigger }}</dd></div>
          <div><dt>Vintage</dt><dd>{{ run.vintage ?? '—' }}</dd></div>
          <div><dt>Rows</dt><dd>{{ run.rows_total === null ? '—' : fmtNum(run.rows_total) }}</dd></div>
          <div>
            <dt>Changes</dt>
            <dd class="counts">
              <span class="added" title="Added">+{{ fmtNum(run.added) }}</span>
              <span class="altered" title="Altered">~{{ fmtNum(run.altered) }}</span>
              <span class="removed" title="Removed">−{{ fmtNum(run.removed) }}</span>
            </dd>
          </div>
        </dl>
        <p v-if="run.error" class="error-box" role="alert"><strong>Error</strong><span class="mono">{{ run.error }}</span></p>
        <div v-if="run.top_indicators.length" class="top">
          <span class="muted">Top indicators</span>
          <ul>
            <li v-for="t in run.top_indicators" :key="t.indicator"><span class="mono">{{ t.indicator }}</span> <span class="muted">{{ fmtNum(t.count) }}</span></li>
          </ul>
        </div>
      </section>

      <section v-if="isHeld" class="banner" role="region" aria-label="Held for review">
        <p>These changes are held for review — {{ fmtNum(run.removed) }} of {{ fmtNum(existing) }} rows would be removed</p>
        <div class="toolbar">
          <button type="button" class="btn" :disabled="busy" @click="confirm = 'approve'">Approve</button>
          <button type="button" class="btn danger" :disabled="busy" @click="confirm = 'reject'">Reject</button>
        </div>
      </section>
      <section v-else-if="isApplying" class="banner applying" role="status">
        <p>Approval queued — applying the held changes…</p>
      </section>
      <p v-if="pollNote" class="muted" role="status">{{ pollNote }}</p>

      <section class="changes" aria-label="Changes">
        <h2>Changes</h2>
        <form class="filters" @submit.prevent="applyFilters">
          <div class="field">
            <label for="c-type">Type</label>
            <select id="c-type" v-model="fType" @change="applyFilters">
              <option value="">All types</option><option value="added">Added</option><option value="altered">Altered</option><option value="removed">Removed</option>
            </select>
          </div>
          <div class="field"><label for="c-ind">Indicator</label><input id="c-ind" v-model="fIndicator" type="search" placeholder="exact code" @change="applyFilters" /></div>
          <div class="field"><label for="c-cty">Country</label><input id="c-cty" v-model="fCountry" type="search" placeholder="e.g. AT" @change="applyFilters" /></div>
        </form>
        <div v-if="changesError" class="toolbar">
          <p class="error-text" role="alert">{{ changesError }}</p>
          <button type="button" class="btn secondary" @click="loadChanges">Retry</button>
        </div>
        <DataTable :columns="columns" :rows="changes" :loading="changesLoading" empty="No changes match these filters.">
          <template #cell-change_type="{ row }">
            <span :class="row.change_type" class="type"><span aria-hidden="true">{{ SIGN[row.change_type as ChangeType] }}</span> {{ row.change_type }}</span>
          </template>
          <template #cell-indicator="{ row }"><span class="mono">{{ row.indicator }}</span></template>
          <template #cell-dims="{ row }">{{ fmtDims(row.dims) }}</template>
          <template #cell-values="{ row }">
            <div v-if="row.excerpt" class="excerpt mono" data-test="excerpt">
              <div v-for="(l, i) in row.excerpt.added" :key="`a${i}`" class="ln add">+ {{ l }}</div>
              <div v-for="(l, i) in row.excerpt.removed" :key="`r${i}`" class="ln rem">− {{ l }}</div>
              <span v-if="!row.excerpt.added.length && !row.excerpt.removed.length" class="muted">No text excerpt</span>
            </div>
            <span v-else class="vals"><span class="old" :title="row.old ?? undefined">{{ fmtValue(row.old) }}</span> → <span :title="row.new ?? undefined">{{ fmtValue(row.new) }}</span></span>
          </template>
        </DataTable>
        <Pager :page="page" :page-size="PAGE_SIZE" :total="changesTotal" @update:page="setPage" />
      </section>

      <Attribution :text="attribution" />
    </template>

    <ConfirmDialog :open="confirm === 'approve'" title="Approve these changes?" confirm-label="Approve"
      :message="`${run ? fmtNum(run.removed) : 0} rows would be removed. The changes are applied in the background.`"
      @confirm="doApprove" @cancel="confirm = null" />
    <ConfirmDialog :open="confirm === 'reject'" title="Reject these changes?" confirm-label="Reject" danger
      message="The held changes are discarded and the data stays as it is."
      @confirm="doReject" @cancel="confirm = null" />
  </div>
</template>

<style scoped>
.src { font-weight: 500; font-size: 1.1rem; }
.meta { margin: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 12px 16px; }
dt { font-size: 0.75rem; color: var(--text-muted); }
dd { margin: 0; font-weight: 600; overflow-wrap: anywhere; }
.counts { display: flex; gap: 10px; }
.added { color: var(--chg-added); }
.altered { color: var(--chg-altered); }
.removed { color: var(--chg-removed); }
.error-box { margin-top: 12px; padding: 8px 12px; border: 1px solid var(--status-fail); border-radius: var(--radius); color: var(--status-fail); display: flex; flex-direction: column; gap: 2px; overflow-wrap: anywhere; }
.top { margin-top: 12px; display: flex; flex-wrap: wrap; gap: 4px 12px; align-items: baseline; }
.top ul { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 4px 14px; }
.banner { border: 1px solid var(--status-warn); border-left-width: 4px; border-radius: var(--radius); background: var(--surface); padding: 12px 16px; display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; justify-content: space-between; }
.banner p { font-weight: 600; min-width: 0; }
.changes { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.filters { display: flex; flex-wrap: wrap; gap: 12px 16px; align-items: flex-end; }
.type { font-weight: 600; white-space: nowrap; }
.type.added { color: var(--chg-added); }
.type.altered { color: var(--chg-altered); }
.type.removed { color: var(--chg-removed); }
.old { color: var(--text-muted); }
.vals { overflow-wrap: anywhere; }
.excerpt { font-size: 0.8125rem; background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius); padding: 6px 8px; min-width: 220px; max-width: 560px; white-space: pre-wrap; overflow-wrap: anywhere; }
.ln.add { color: var(--chg-added); }
.ln.rem { color: var(--chg-removed); }
</style>
