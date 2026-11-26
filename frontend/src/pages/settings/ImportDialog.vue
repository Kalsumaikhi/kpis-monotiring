<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ApiError, apiSend, apiUpload } from '../../api/client'
import type { ImportConfirmResult, ImportPreview, ImportRowStatus } from '../../api/types'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: []; imported: [] }>()

const TEMPLATE = 'Source name,KPI name,Link,Key name\r\nAcme,Pricing page,https://example.com/pricing,\r\n'
const CHIP: Record<ImportRowStatus, { label: string; tone: 'ok' | 'muted' | 'fail'; mark: string }> = {
  new_source: { label: 'New source', tone: 'ok', mark: '+' },
  existing_source: { label: 'Existing source', tone: 'ok', mark: '✓' },
  duplicate: { label: 'Duplicate — skipped', tone: 'muted', mark: '–' },
  invalid: { label: 'Invalid', tone: 'fail', mark: '✕' },
}

const preview = ref<ImportPreview | null>(null)
const result = ref<ImportConfirmResult | null>(null)
const error = ref('')
const busy = ref(false)
const dialog = ref<HTMLElement | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
let opener: HTMLElement | null = null
let reqId = 0
const tryClose = () => { if (!busy.value) emit('close') }

const importable = computed(() => preview.value?.rows.filter((r) => r.status === 'new_source' || r.status === 'existing_source').length ?? 0)
const skipped = computed(() => preview.value?.rows.filter((r) => r.status === 'duplicate').length ?? 0)
const invalid = computed(() => preview.value?.rows.filter((r) => r.status === 'invalid').length ?? 0)

function reset() {
  preview.value = null
  result.value = null
  error.value = ''
  if (fileInput.value) fileInput.value.value = ''
}

function downloadTemplate() {
  const url = URL.createObjectURL(new Blob([TEMPLATE], { type: 'text/csv;charset=utf-8' }))
  const a = document.createElement('a')
  a.href = url
  a.download = 'kpi-sources-template.csv'
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

async function onFile(ev: Event) {
  const file = (ev.target as HTMLInputElement).files?.[0]
  if (!file) return
  const mine = ++reqId
  busy.value = true
  error.value = ''
  preview.value = null
  result.value = null
  try {
    const fd = new FormData()
    fd.append('file', file)
    const res = await apiUpload<ImportPreview>('/api/watch-targets/import/preview', fd)
    if (mine === reqId) preview.value = res
  } catch (e) {
    if (mine !== reqId) return
    error.value = e instanceof ApiError
      ? (e.status === 413 ? 'That file is too large (max 2 MB).' : e.detail)
      : 'Could not read the file'
  } finally {
    if (mine === reqId) busy.value = false
  }
}

async function confirm() {
  if (!preview.value || importable.value === 0) return
  const mine = ++reqId
  busy.value = true
  error.value = ''
  try {
    const res = await apiSend<ImportConfirmResult>('POST', '/api/watch-targets/import/confirm', { token: preview.value.token })
    // The import happened server-side even if the dialog was closed meanwhile: always refresh the list.
    emit('imported')
    if (mine !== reqId) return
    result.value = res
    preview.value = null
  } catch (e) {
    if (mine !== reqId) return
    if (e instanceof ApiError && e.status === 410) {
      error.value = 'Preview expired — upload again'
      preview.value = null
      if (fileInput.value) fileInput.value.value = ''
    } else {
      error.value = e instanceof ApiError ? e.detail : 'Import failed'
    }
  } finally {
    if (mine === reqId) busy.value = false
  }
}

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); tryClose(); return }
  if (e.key !== 'Tab' || !dialog.value) return
  const items = Array.from(dialog.value.querySelectorAll<HTMLElement>(FOCUSABLE))
  if (!items.length) return
  const active = document.activeElement as HTMLElement | null
  if (e.shiftKey && (active === items[0] || !dialog.value.contains(active))) { e.preventDefault(); items[items.length - 1].focus() }
  else if (!e.shiftKey && (active === items[items.length - 1] || !dialog.value.contains(active))) { e.preventDefault(); items[0].focus() }
}

watch(() => props.open, async (o) => {
  if (o) {
    reqId++
    busy.value = false
    reset()
    opener = document.activeElement as HTMLElement | null
    document.addEventListener('keydown', onKey, true)
    await nextTick()
    fileInput.value?.focus()
  } else {
    document.removeEventListener('keydown', onKey, true)
    reqId++ // late responses after close are ignored
    busy.value = false
    const target = opener && opener.isConnected ? opener : document.querySelector<HTMLElement>('main')
    target?.focus()
    opener = null
  }
}, { immediate: true })
onBeforeUnmount(() => document.removeEventListener('keydown', onKey, true))
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="overlay" @click.self="tryClose">
      <div ref="dialog" class="dialog" role="dialog" aria-modal="true" aria-labelledby="imp-title">
        <h2 id="imp-title">Import KPIs from Excel or CSV</h2>
        <p class="muted">One row per KPI with the columns <code>Source name</code>, <code>KPI name</code> and <code>Link</code>, plus an optional <code>Key name</code> (a key saved in Settings → Keys).
          <button type="button" class="linklike" data-test="template" @click="downloadTemplate">Download template</button></p>

        <div class="field">
          <label for="imp-file">File (.xlsx or .csv)</label>
          <input id="imp-file" ref="fileInput" type="file" accept=".xlsx,.csv" :disabled="busy" @change="onFile" />
        </div>

        <p v-if="busy" class="muted" role="status">Working…</p>
        <p v-if="error" class="error-text" role="alert">{{ error }}</p>

        <template v-if="preview">
          <p role="status" class="summary">
            <strong>{{ importable }}</strong> to import<template v-if="skipped">, {{ skipped }} skipped (duplicates)</template><template v-if="invalid">, {{ invalid }} invalid</template>.
          </p>
          <div class="table-wrap" tabindex="0" aria-label="Import preview">
            <table>
              <thead><tr><th scope="col">Row</th><th scope="col">Source</th><th scope="col">KPI</th><th scope="col">Link</th><th scope="col">Key</th><th scope="col">Status</th></tr></thead>
              <tbody>
                <tr v-for="r in preview.rows" :key="r.row" :class="{ dim: r.status === 'duplicate' }">
                  <td>{{ r.row }}</td>
                  <td>{{ r.source_name }}</td>
                  <td>{{ r.kpi_name }}</td>
                  <td class="url">{{ r.url }}</td>
                  <td data-test="key-cell">{{ r.key_name || '—' }}</td>
                  <td>
                    <span class="chip" :class="`tone-${CHIP[r.status]?.tone ?? 'muted'}`"><span aria-hidden="true">{{ CHIP[r.status]?.mark }}</span> {{ CHIP[r.status]?.label ?? r.status }}</span>
                    <div v-if="r.error" class="error-text small">{{ r.error }}</div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="muted small">New KPIs are checked silently the first time — you'll be alerted from the second change on.</p>
        </template>

        <div v-if="result" role="status" class="done">
          <strong>Import finished.</strong>
          Created {{ result.created_targets }} KPIs in {{ result.created_sources }} new sources; {{ result.skipped }} skipped.
          <template v-if="result.skipped_rows?.length">
            <p class="small">Changed since the preview, so not imported:</p>
            <ul class="small" data-test="skipped-rows">
              <li v-for="s in result.skipped_rows" :key="`${s.row}-${s.kpi_name}`">Row {{ s.row }} ({{ s.source_name }} / {{ s.kpi_name }}): {{ s.reason }}</li>
            </ul>
          </template>
          <p class="muted small">New KPIs are checked silently the first time — you'll be alerted from the second change on.</p>
        </div>

        <div class="actions">
          <button type="button" class="btn secondary" :disabled="busy" @click="emit('close')">{{ result ? 'Close' : 'Cancel' }}</button>
          <button v-if="!result" type="button" class="btn" :disabled="busy || importable === 0" @click="confirm">Import {{ importable }} KPIs</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.overlay { position: fixed; inset: 0; background: var(--overlay); display: flex; align-items: center; justify-content: center; padding: 16px; z-index: 50; }
.dialog { background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px; width: min(720px, 100%); max-height: 100%; overflow: auto; display: flex; flex-direction: column; gap: 12px; }
.linklike { all: unset; cursor: pointer; color: var(--link); text-decoration: underline; text-underline-offset: 2px; }
.linklike:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.table-wrap { position: relative; overflow: auto; max-height: 45vh; border: 1px solid var(--border); border-radius: var(--radius); }
table { width: 100%; border-collapse: collapse; font-size: 0.8125rem; }
th, td { padding: 6px 8px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: top; }
th { position: sticky; top: 0; background: var(--surface-2); color: var(--text-muted); }
.url { overflow-wrap: anywhere; min-width: 10rem; }
.dim { color: var(--text-muted); }
.chip { display: inline-flex; gap: 4px; padding: 0 8px; border: 1px solid currentColor; border-radius: 999px; font-weight: 600; white-space: nowrap; }
.tone-ok { color: var(--status-ok); }
.tone-muted { color: var(--text-muted); }
.tone-fail { color: var(--status-fail); }
.small { font-size: 0.8125rem; }
.actions { display: flex; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }
</style>
