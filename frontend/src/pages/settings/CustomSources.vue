<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { ApiError, apiGet, apiSend } from '../../api/client'
import type { CustomSource, WatchTarget } from '../../api/types'
import ConfirmDialog from '../../components/ConfirmDialog.vue'
import StatusBadge from '../../components/StatusBadge.vue'
import { useToast } from '../../composables/useToast'
import { absoluteTime, relativeTime } from '../../lib/format'
import ImportDialog from './ImportDialog.vue'
import TargetForm from './TargetForm.vue'

const { show } = useToast()
const sources = ref<CustomSource[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const form = ref<{ key: string; target: WatchTarget | null; sourceName: string; advanced: boolean } | null>(null)
const formWrap = ref<HTMLElement | null>(null)
const importOpen = ref(false)
const toDelete = ref<WatchTarget | null>(null)
const busyId = ref<number | null>(null)
const note = ref('')
const formComp = ref<{ dirty: boolean } | null>(null)
type FormReq = { target: WatchTarget | null; sourceName: string; advanced: boolean }
const pendingForm = ref<FormReq | null>(null)
const addBtn = ref<HTMLButtonElement | null>(null)

const sourceNames = computed(() => sources.value.map((s) => s.name))

async function load() {
  try {
    sources.value = await apiGet<CustomSource[]>('/api/custom-sources')
    error.value = null
  } catch (e) {
    error.value = e instanceof ApiError ? e.detail : 'Could not load custom sources'
  } finally {
    loading.value = false
  }
}
onMounted(load)

async function openForm(f: FormReq, force = false) {
  if (!force && form.value && formComp.value?.dirty) {
    pendingForm.value = f
    return
  }
  form.value = { key: `${f.target?.id ?? 'new'}-${Date.now()}`, ...f }
  await nextTick()
  formWrap.value?.scrollIntoView?.({ block: 'nearest' })
}
const openAdd = () => openForm({ target: null, sourceName: '', advanced: false })
const openEdit = (s: CustomSource, t: WatchTarget, advanced = false) => openForm({ target: t, sourceName: s.name, advanced })

async function closeForm(targetId: number | null) {
  form.value = null
  await nextTick()
  const el = targetId !== null ? document.querySelector<HTMLElement>(`[data-test=edit-${targetId}]`) : null
  ;(el ?? addBtn.value)?.focus()
}
function discardAndOpen() {
  const f = pendingForm.value
  pendingForm.value = null
  if (f) void openForm(f, true)
}

async function onSaved() {
  const adding = !form.value?.target
  const id = form.value?.target?.id ?? null
  await load()
  await closeForm(id)
  show(adding ? 'KPI added' : 'KPI saved')
  if (adding) note.value = "New KPIs are checked silently the first time — you'll be alerted from the second change on."
}

async function setEnabled(t: WatchTarget, enabled: boolean) {
  busyId.value = t.id
  try {
    await apiSend('PATCH', `/api/watch-targets/${t.id}`, { enabled })
    t.enabled = enabled
    note.value = enabled ? '' : 'Disabled KPIs keep no data; re-enabling starts a fresh baseline.'
    show(enabled ? 'KPI enabled. A fresh baseline starts at the next check.' : 'KPI disabled')
  } catch (e) {
    show(e instanceof ApiError ? e.detail : 'Could not update', 'error')
  } finally {
    busyId.value = null
  }
}

async function doDelete() {
  const t = toDelete.value
  toDelete.value = null
  if (!t) return
  busyId.value = t.id
  try {
    await apiSend('DELETE', `/api/watch-targets/${t.id}`)
    show('KPI deleted')
    await load()
  } catch (e) {
    show(e instanceof ApiError ? e.detail : 'Could not delete', 'error')
  } finally {
    busyId.value = null
  }
}

async function onImported() {
  await load()
  note.value = "New KPIs are checked silently the first time — you'll be alerted from the second change on."
}

const shortUrl = (u: string) => u.replace(/^https?:\/\//i, '')
</script>

<template>
  <section class="card" aria-labelledby="custom-h">
    <div class="head">
      <div>
        <h2 id="custom-h">Custom sources</h2>
        <p class="muted">Web pages and API links you want to watch. Each KPI is one link.</p>
      </div>
      <div class="toolbar">
        <button ref="addBtn" type="button" class="btn" data-test="add" @click="openAdd">Add KPI</button>
        <button type="button" class="btn secondary" data-test="import" @click="importOpen = true">Import from Excel</button>
      </div>
    </div>

    <p v-if="note" class="note" role="status">{{ note }}</p>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p>

    <div v-if="form" ref="formWrap" class="formwrap">
      <TargetForm ref="formComp" :key="form.key" :source-names="sourceNames" :target="form.target" :source-name="form.sourceName" :open-advanced="form.advanced" @saved="onSaved" @cancel="closeForm(form.target?.id ?? null)" />
    </div>

    <p v-if="loading" class="muted">Loading…</p>
    <p v-else-if="!sources.length && !error" class="muted empty">No custom sources yet. Use “Add KPI” or “Import from Excel” to start watching a link.</p>

    <section v-for="s in sources" :key="s.slug" class="group" :aria-label="s.name">
      <h3>{{ s.name }} <span class="muted count">{{ s.targets.length }} {{ s.targets.length === 1 ? 'KPI' : 'KPIs' }}</span></h3>
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th scope="col">KPI</th><th scope="col">Link</th><th scope="col">Status</th><th scope="col">Last checked</th><th scope="col"><span class="sr-only">Actions</span></th></tr>
          </thead>
          <tbody>
            <tr v-for="t in s.targets" :key="t.id" :class="{ off: !t.enabled }">
              <td>
                <strong>{{ t.kpi_name }}</strong>
                <div v-if="!t.enabled" class="muted small">Disabled</div>
                <button v-if="t.noisy" type="button" class="noisy" :data-test="`noisy-${t.id}`" @click="openEdit(s, t, true)">⚠ Noisy — set a focus area</button>
              </td>
              <td class="url"><a :href="t.url" target="_blank" rel="noopener noreferrer" :title="t.url">{{ shortUrl(t.url) }}</a></td>
              <td>
                <StatusBadge :status="t.last_status" />
                <div v-if="t.last_error" class="error-text small">{{ t.last_error }}</div>
              </td>
              <td><span :title="absoluteTime(t.last_checked_at)">{{ relativeTime(t.last_checked_at, 'Never') }}</span></td>
              <td>
                <div class="acts">
                  <button type="button" class="btn secondary" :data-test="`edit-${t.id}`" @click="openEdit(s, t)">Edit</button>
                  <button type="button" class="btn secondary" :data-test="`toggle-${t.id}`" :disabled="busyId === t.id" @click="setEnabled(t, !t.enabled)">{{ t.enabled ? 'Disable' : 'Enable' }}</button>
                  <button type="button" class="btn danger" :data-test="`delete-${t.id}`" :disabled="busyId === t.id" @click="toDelete = t">Delete</button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <ConfirmDialog
      :open="!!toDelete" title="Delete this KPI?" message="Deleting removes this KPI's history. No alert is sent."
      confirm-label="Delete" danger @confirm="doDelete" @cancel="toDelete = null"
    />
    <ConfirmDialog
      :open="!!pendingForm" title="Discard unsaved changes?" message="The form you have open has changes that are not saved."
      confirm-label="Discard changes" danger @confirm="discardAndOpen" @cancel="pendingForm = null"
    />
    <ImportDialog :open="importOpen" @close="importOpen = false" @imported="onImported" />
  </section>
</template>

<style scoped>
.card { display: flex; flex-direction: column; gap: 12px; }
.head { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 12px; align-items: flex-start; }
.note { border-left: 4px solid var(--link); background: var(--surface-2); padding: 8px 12px; border-radius: var(--radius); }
.group { display: flex; flex-direction: column; gap: 8px; }
.count { font-weight: 400; font-size: 0.8125rem; }
.table-wrap { position: relative; overflow-x: auto; max-width: 100%; border: 1px solid var(--border); border-radius: var(--radius); }
table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
th, td { padding: 9px 12px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: top; }
th { background: var(--surface-2); color: var(--text-muted); font-size: 0.8125rem; white-space: nowrap; }
tbody tr:last-child td { border-bottom: 0; }
.off td:not(:last-child) { opacity: 0.7; }
.url { min-width: 11rem; max-width: 18rem; overflow-wrap: anywhere; }
.small { font-size: 0.8125rem; }
.acts { display: flex; gap: 6px; flex-wrap: wrap; }
.acts .btn { min-height: 32px; padding: 0 10px; }
.noisy { display: block; width: fit-content; margin-top: 6px; white-space: nowrap; margin-top: 4px; font: inherit; font-size: 0.8125rem; font-weight: 600; cursor: pointer; background: transparent; color: var(--status-warn); border: 1px solid currentColor; border-radius: 999px; padding: 0 10px; }
.noisy:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.empty { padding: 12px 0; }
</style>
