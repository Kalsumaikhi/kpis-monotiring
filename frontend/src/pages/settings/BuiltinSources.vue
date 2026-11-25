<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ApiError, apiGet, apiSend } from '../../api/client'
import type { RunNowResult, Source, SourcePatchResult, StoredKey } from '../../api/types'
import { fetchKeys } from '../../composables/useKeys'
import StatusBadge from '../../components/StatusBadge.vue'
import { useToast } from '../../composables/useToast'
import { absoluteTime, relativeTime } from '../../lib/format'

const { show } = useToast()
const sources = ref<Source[]>([])
const keys = ref<StoredKey[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const cron = reactive<Record<string, string>>({})
const cronError = reactive<Record<string, string>>({})
const busy = reactive<Record<string, boolean>>({})

const PRESETS = [
  { label: 'Daily 03:00', value: '0 3 * * *' },
  { label: 'Weekly Mon 03:00', value: '0 3 * * 1' },
]

async function load() {
  loading.value = true
  error.value = null
  try {
    const list = (await apiGet<Source[]>('/api/sources')).filter((s) => s.access !== 'watch')
    // Keys first: a <select> bound before its options exist would show "None" for a source that has a key.
    keys.value = list.some((s) => s.credential_supported) ? await fetchKeys() : []
    sources.value = list
    for (const s of list) cron[s.slug] = s.cron ?? ''
  } catch (e) {
    error.value = e instanceof ApiError ? e.detail : 'Could not load sources'
  } finally {
    loading.value = false
  }
}
onMounted(load)

async function patch(s: Source, body: { enabled?: boolean; cron?: string }) {
  busy[s.slug] = true
  cronError[s.slug] = ''
  try {
    const res = await apiSend<SourcePatchResult>('PATCH', `/api/sources/${s.slug}`, body)
    s.enabled = res.enabled
    s.cron = res.cron
    cron[s.slug] = res.cron ?? ''
    show(`${s.name} updated`)
  } catch (e) {
    if (e instanceof ApiError && e.status === 422 && body.cron !== undefined) cronError[s.slug] = 'Invalid schedule'
    else show(e instanceof ApiError ? e.detail : 'Could not save', 'error')
    if (body.enabled !== undefined) s.enabled = !body.enabled
  } finally {
    busy[s.slug] = false
  }
}

function saveCron(s: Source) {
  const value = (cron[s.slug] ?? '').trim()
  if (busy[s.slug] || value === (s.cron ?? '')) return
  cron[s.slug] = value
  return patch(s, { cron: value })
}

async function toggle(s: Source, ev: Event) {
  const enabled = (ev.target as HTMLInputElement).checked
  s.enabled = enabled
  await patch(s, { enabled })
}

async function setKey(s: Source, ev: Event) {
  const raw = (ev.target as HTMLSelectElement).value
  const id = raw === '' ? null : Number(raw)
  busy[s.slug] = true
  try {
    const res = await apiSend<SourcePatchResult>('PATCH', `/api/sources/${s.slug}`, { credential_id: id })
    s.credential_id = res.credential_id ?? id
    s.credential_name = keys.value.find((k) => k.id === s.credential_id)?.name ?? null
    show(`${s.name} updated`)
  } catch (e) {
    ;(ev.target as HTMLSelectElement).value = s.credential_id === null ? '' : String(s.credential_id)
    show(e instanceof ApiError ? e.detail : 'Could not save', 'error')
  } finally {
    busy[s.slug] = false
  }
}

async function runNow(s: Source) {
  busy[s.slug] = true
  try {
    const res = await apiSend<RunNowResult>('POST', `/api/sources/${s.slug}/run`)
    show(`Run #${res.run_id} started for ${s.name}`)
  } catch (e) {
    show(e instanceof ApiError ? e.detail : 'Could not start the run', 'error')
  } finally {
    busy[s.slug] = false
  }
}
</script>

<template>
  <section class="card" aria-labelledby="builtin-h">
    <h2 id="builtin-h">Built-in sources</h2>
    <p class="muted intro">Choose which sources are checked automatically and when. Schedules use cron syntax (minute hour day month weekday).</p>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th scope="col">Source</th>
            <th scope="col">Enabled</th>
            <th scope="col">Schedule</th>
            <th v-if="sources.some((x) => x.credential_supported)" scope="col">Key</th>
            <th scope="col">Last run</th>
            <th scope="col"><span class="sr-only">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading && !sources.length"><td colspan="6" class="muted state">Loading…</td></tr>
          <tr v-else-if="!sources.length"><td colspan="6" class="muted state">No built-in sources.</td></tr>
          <tr v-for="s in sources" :key="s.slug">
            <td><strong>{{ s.name }}</strong></td>
            <td>
              <label class="switch">
                <input type="checkbox" :checked="s.enabled" :disabled="busy[s.slug]" :aria-label="`${s.name} enabled`" @change="toggle(s, $event)" />
                <span>{{ s.enabled ? 'On' : 'Off' }}</span>
              </label>
            </td>
            <td>
              <div class="cron">
                <input
                  v-model="cron[s.slug]" type="text" class="mono" :data-test="`cron-${s.slug}`" :aria-label="`${s.name} schedule`"
                  :aria-invalid="cronError[s.slug] ? 'true' : 'false'" :aria-describedby="cronError[s.slug] ? `ce-${s.slug}` : undefined"
                  @keydown.enter.prevent="saveCron(s)"
                />
                <button type="button" class="btn secondary" :data-test="`save-${s.slug}`" :disabled="busy[s.slug] || (cron[s.slug] ?? '').trim() === (s.cron ?? '')" @click="saveCron(s)">Save</button>
              </div>
              <div class="presets">
                <button v-for="p in PRESETS" :key="p.value" type="button" class="chip" @click="cron[s.slug] = p.value">{{ p.label }}</button>
              </div>
              <p v-if="cronError[s.slug]" :id="`ce-${s.slug}`" class="error-text" role="alert">{{ cronError[s.slug] }}</p>
            </td>
            <td v-if="sources.some((x) => x.credential_supported)">
              <select
                v-if="s.credential_supported" :value="s.credential_id === null ? '' : String(s.credential_id)" :data-test="`key-${s.slug}`"
                :aria-label="`${s.name} key`" :disabled="busy[s.slug]" @change="setKey(s, $event)"
              >
                <option value="">None</option>
                <option v-for="k in keys" :key="k.id" :value="String(k.id)">{{ k.name }}</option>
              </select>
              <span v-else class="muted" aria-label="No key option">—</span>
            </td>
            <td>
              <template v-if="s.last_run">
                <StatusBadge :status="s.last_run.status" />
                <div class="muted small" :title="absoluteTime(s.last_run.finished_at)">{{ relativeTime(s.last_run.finished_at) }}</div>
              </template>
              <span v-else class="muted">Never run</span>
            </td>
            <td><button type="button" class="btn secondary" :data-test="`run-${s.slug}`" :disabled="busy[s.slug]" @click="runNow(s)">Run now</button></td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.intro { margin: 4px 0 12px; }
.table-wrap { position: relative; overflow-x: auto; max-width: 100%; border: 1px solid var(--border); border-radius: var(--radius); }
table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
th, td { padding: 9px 12px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: top; }
th { background: var(--surface-2); color: var(--text-muted); font-size: 0.8125rem; white-space: nowrap; }
tbody tr:last-child td { border-bottom: 0; }
.state { text-align: center; padding: 24px; }
.small { font-size: 0.8125rem; }
.switch { display: inline-flex; align-items: center; gap: 6px; font-weight: 500; cursor: pointer; min-height: 38px; }
.switch input { width: 18px; height: 18px; accent-color: var(--accent); }
.cron { display: flex; gap: 6px; align-items: center; }
.cron input { width: 9.5rem; }
.presets { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.chip { all: unset; cursor: pointer; font-size: 0.8125rem; color: var(--link); border: 1px solid var(--border); border-radius: 999px; padding: 1px 10px; }
.chip:hover { border-color: var(--link); }
.chip:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
</style>
