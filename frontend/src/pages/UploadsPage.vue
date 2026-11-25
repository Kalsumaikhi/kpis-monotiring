<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { ApiError, apiGet, apiUpload } from '../api/client'
import type { Source, UploadResult, UploadRow } from '../api/types'
import DataTable, { type Column } from '../components/DataTable.vue'
import EmptyState from '../components/EmptyState.vue'
import Pager from '../components/Pager.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { absoluteTime, relativeTime } from '../lib/format'

const MAX_BYTES = 50 * 1024 * 1024
const PAGE_SIZE = 20
const NAME_KEY = 'kpi.uploader'

const sources = ref<Source[]>([])
const loadingSources = ref(true)
const sourcesError = ref<string | null>(null)
const slug = ref('')
const who = ref('')
const file = ref<File | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const uploading = ref(false)
const error = ref<string | null>(null)
const resultRun = ref<number | null>(null)

const items = ref<UploadRow[]>([])
const total = ref(0)
const page = ref(1)
const listLoading = ref(false)
const listError = ref<string | null>(null)

const columns: Column[] = [
  { key: 'created_at', label: 'When' },
  { key: 'source', label: 'Source' },
  { key: 'filename', label: 'File' },
  { key: 'who', label: 'Uploaded by' },
  { key: 'run_status', label: 'Status' },
  { key: 'run_id', label: '' },
]

const sizeError = computed(() => (file.value && file.value.size > MAX_BYTES ? 'File is larger than 50 MB' : null))
const canSubmit = computed(() => !!slug.value && !!who.value.trim() && !!file.value && !sizeError.value && !uploading.value)

function onFile(e: Event) {
  file.value = (e.target as HTMLInputElement).files?.[0] ?? null
  resultRun.value = null
  error.value = null
}

async function loadList() {
  listLoading.value = true
  try {
    const res = await apiGet<{ items: UploadRow[]; total: number }>('/api/uploads', { page: page.value, page_size: PAGE_SIZE })
    items.value = res.items
    total.value = res.total
    listError.value = null
  } catch (e) {
    listError.value = e instanceof ApiError ? e.detail : 'Could not load past uploads'
  } finally {
    listLoading.value = false
  }
}

function setPage(p: number) { page.value = p; void loadList() }

async function submit() {
  if (!canSubmit.value || !file.value) return
  uploading.value = true
  error.value = null
  resultRun.value = null
  const fd = new FormData()
  fd.append('source', slug.value)
  fd.append('who', who.value.trim())
  fd.append('file', file.value)
  try {
    const res = await apiUpload<UploadResult>('/api/uploads', fd)
    resultRun.value = res.run_id
    try { localStorage.setItem(NAME_KEY, who.value.trim()) } catch { /* per-viewer convenience only */ }
    file.value = null
    if (fileInput.value) fileInput.value.value = ''
    page.value = 1
    void loadList()
  } catch (e) {
    if (e instanceof ApiError && e.status === 413) error.value = 'File is larger than 50 MB'
    else error.value = e instanceof ApiError ? e.detail : 'Upload failed'
  } finally {
    uploading.value = false
  }
}

onMounted(async () => {
  try { who.value = localStorage.getItem(NAME_KEY) ?? '' } catch { /* ignore */ }
  void loadList()
  try {
    sources.value = await apiGet<Source[]>('/api/sources', { access: 'manual' })
    if (sources.value.length === 1) slug.value = sources.value[0].slug
  } catch (e) {
    sourcesError.value = e instanceof ApiError ? e.detail : 'Could not load sources'
  } finally {
    loadingSources.value = false
  }
})
</script>

<template>
  <section class="uploads">
    <h1>Uploads</h1>
    <p v-if="loadingSources" class="muted">Loading…</p>
    <p v-else-if="sourcesError" class="err" role="alert">{{ sourcesError }}</p>

    <EmptyState v-else-if="!sources.length" title="No upload sources configured yet. Sources like TheGlobalEconomy.com are added once a sample export is available.">
      <p class="muted">Want to watch web pages or API links instead? Add them in <RouterLink to="/settings">Settings</RouterLink>.</p>
    </EmptyState>

    <form v-else class="card form" @submit.prevent="submit">
      <div class="field">
        <label for="up-source">Source</label>
        <select id="up-source" v-model="slug" required>
          <option value="" disabled>Choose a source</option>
          <option v-for="s in sources" :key="s.slug" :value="s.slug">{{ s.name }}</option>
        </select>
      </div>
      <div class="field">
        <label for="up-who">Your name</label>
        <input id="up-who" v-model="who" type="text" autocomplete="name" required />
      </div>
      <div class="field">
        <label for="up-file">File</label>
        <input id="up-file" ref="fileInput" type="file" required :aria-describedby="sizeError ? 'up-size' : undefined" @change="onFile" />
        <p v-if="sizeError" id="up-size" class="err" role="alert">{{ sizeError }}</p>
      </div>
      <div class="actions">
        <button type="submit" class="btn" :disabled="!canSubmit">{{ uploading ? 'Uploading…' : 'Upload' }}</button>
        <span v-if="uploading" class="muted progress" role="status"><span class="bar" aria-hidden="true"></span>Uploading… please keep this page open.</span>
      </div>
      <p v-if="error" class="err" role="alert">{{ error }}</p>
      <div v-if="resultRun !== null" class="success" role="status">
        <strong>Upload received.</strong> It is waiting for your review before anything changes.
        <RouterLink :to="`/runs/${resultRun}`">Review changes</RouterLink>
      </div>
    </form>

    <h2>Past uploads</h2>
    <p v-if="listError" class="err" role="alert">{{ listError }}</p>
    <DataTable :columns="columns" :rows="items" :loading="listLoading" empty="No uploads yet." row-key="id">
      <template #cell-created_at="{ row }"><span :title="absoluteTime(row.created_at)">{{ relativeTime(row.created_at) }}</span></template>
      <template #cell-run_status="{ row }"><StatusBadge v-if="row.run_status" :status="row.run_status" /><span v-else class="muted">—</span></template>
      <template #cell-run_id="{ row }"><RouterLink v-if="row.run_id" :to="`/runs/${row.run_id}`">View run</RouterLink></template>
    </DataTable>
    <Pager :page="page" :page-size="PAGE_SIZE" :total="total" @update:page="setPage" />
  </section>
</template>

<style scoped>
.uploads { display: flex; flex-direction: column; gap: 16px; }
.form { display: flex; flex-direction: column; gap: 14px; max-width: 520px; padding: 16px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; }
.field { display: flex; flex-direction: column; gap: 4px; }
.actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.err { color: var(--status-fail); }
.success { padding: 10px 12px; border: 1px solid var(--status-ok); border-radius: 6px; display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: center; }
.bar { display: inline-block; width: 64px; height: 4px; margin-right: 8px; vertical-align: middle; border-radius: 2px; background: linear-gradient(90deg, var(--border) 0 30%, var(--accent) 30% 60%, var(--border) 60%); background-size: 200% 100%; animation: slide 1.2s linear infinite; }
@keyframes slide { to { background-position: -200% 0; } }
@media (prefers-reduced-motion: reduce) { .bar { animation: none; } }
</style>
