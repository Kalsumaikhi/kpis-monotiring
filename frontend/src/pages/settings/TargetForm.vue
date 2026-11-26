<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { ApiError, apiSend } from '../../api/client'
import { fetchKeys } from '../../composables/useKeys'
import type { StoredKey, TargetIn, TargetPatch, TestLinkResult, WatchKind, WatchTarget } from '../../api/types'

const props = defineProps<{ sourceNames: string[]; target?: WatchTarget | null; sourceName?: string; openAdvanced?: boolean }>()
const emit = defineEmits<{ saved: []; cancel: [] }>()

const editing = computed(() => !!props.target)
const source = ref(props.sourceName ?? '')
const kpi = ref(props.target?.kpi_name ?? '')
const url = ref(props.target?.url ?? '')
const selector = ref(props.target?.selector ?? '')
const kind = ref<WatchKind>(props.target?.kind ?? 'auto')
const useBrowser = ref(props.target?.use_browser ?? false)
const advanced = ref(!!props.openAdvanced || !!props.target?.selector || props.target?.kind === 'api' || !!props.target?.use_browser)
const credentialId = ref<number | null>(props.target?.credential_id ?? null)
const keys = ref<StoredKey[]>([])
const isLogin = computed(() => keys.value.find((k) => k.id === credentialId.value)?.kind === 'login')
// A login key always runs in the browser (the server sets this too).
let browserBeforeLogin: boolean | null = null
watch(isLogin, (v) => {
  if (v) {
    browserBeforeLogin = useBrowser.value
    useBrowser.value = true
  } else if (browserBeforeLogin !== null) {
    useBrowser.value = browserBeforeLogin // give back what the user had before the login forced it
    browserBeforeLogin = null
  }
}, { flush: 'sync' })
const saving = ref(false)
const saveError = ref('')
const testing = ref(false)
const testResult = ref<TestLinkResult | null>(null)
const testError = ref('')
// "key rejected (HTTP 403)" -> "403"; the generic wording when the server didn't say.
const rejectedCode = computed(() => /HTTP (\d{3})/.exec(testResult.value?.error ?? '')?.[1] ?? '401/403')
const first = ref<HTMLInputElement | null>(null)

const urlOk = computed(() => /^https?:\/\/[^\s/]+\S*$/i.test(url.value.trim()))
// The server enforces these too; blocking here saves a round trip.
const keyProblem = computed(() => {
  if (credentialId.value === null) return ''
  if (/^http:\/\//i.test(url.value.trim())) return 'keys can only be sent over https'
  if (useBrowser.value && !isLogin.value) return 'keys can\'t be used with "Needs a browser" yet'
  return ''
})
const valid = computed(() => source.value.trim() !== '' && kpi.value.trim() !== '' && urlOk.value && !keyProblem.value)
const urlTouched = ref(false)

const initial = JSON.stringify([source.value, kpi.value, url.value, selector.value, kind.value, useBrowser.value, credentialId.value])
const dirty = computed(() => JSON.stringify([source.value, kpi.value, url.value, selector.value, kind.value, useBrowser.value, credentialId.value]) !== initial)
defineExpose({ dirty })
// Any change to what is tested makes an old preview misleading.
watch([url, selector, kind, useBrowser, credentialId], () => { testResult.value = null; testError.value = '' })

onMounted(async () => {
  void fetchKeys().then((k) => { keys.value = k })
  await nextTick()
  if (props.openAdvanced) document.getElementById('tf-selector')?.focus()
  else first.value?.focus()
})

function extras() {
  return {
    kind: kind.value,
    selector: selector.value.trim() || null,
    use_browser: useBrowser.value,
    credential_id: credentialId.value,
  }
}

async function save() {
  if (!valid.value || saving.value) return
  saving.value = true
  saveError.value = ''
  try {
    if (props.target) {
      const body: TargetPatch = { kpi_name: kpi.value.trim(), url: url.value.trim(), ...extras() }
      await apiSend('PATCH', `/api/watch-targets/${props.target.id}`, body)
    } else {
      const body: TargetIn = { source_name: source.value.trim(), kpi_name: kpi.value.trim(), url: url.value.trim(), ...extras() }
      await apiSend('POST', '/api/custom-sources', body)
    }
    emit('saved')
  } catch (e) {
    saveError.value = e instanceof ApiError ? e.detail : 'Could not save'
  } finally {
    saving.value = false
  }
}

async function testLink() {
  urlTouched.value = true
  if (!urlOk.value) return
  testing.value = true
  testResult.value = null
  testError.value = ''
  try {
    testResult.value = await apiSend<TestLinkResult>('POST', '/api/watch-targets/test', { url: url.value.trim(), ...extras() })
  } catch (e) {
    testError.value = e instanceof ApiError ? e.detail : 'Test failed'
  } finally {
    testing.value = false
  }
}

const blockedWords = computed(() => {
  const err = (testResult.value?.error ?? '').toLowerCase()
  if (err.includes('destination')) return "Internal addresses aren't allowed."
  return "This site doesn't allow automated access."
})
const jsonRows = computed(() => (testResult.value?.json_preview ?? []).map((r) => ({ path: r.path, value: typeof r.value === 'string' ? r.value : JSON.stringify(r.value) })))
</script>

<template>
  <form class="card form" novalidate :aria-label="editing ? 'Edit KPI' : 'Add KPI'" @submit.prevent="save">
    <h3>{{ editing ? 'Edit KPI' : 'Add KPI' }}</h3>
    <p class="muted"><span aria-hidden="true">*</span> Required. Watch a web page or an API link and get alerted when it changes.</p>

    <div class="field">
      <label for="tf-source">Source name <span class="req" aria-hidden="true">*</span></label>
      <input id="tf-source" ref="first" v-model="source" type="text" list="tf-sources" autocomplete="off" required :readonly="editing" :aria-readonly="editing" />
      <datalist id="tf-sources"><option v-for="n in sourceNames" :key="n" :value="n" /></datalist>
      <span class="hint muted">{{ editing ? 'The source cannot be changed. Delete and re-add the KPI to move it.' : 'Pick an existing group or type a new name.' }}</span>
    </div>

    <div class="field">
      <label for="tf-kpi">KPI name <span class="req" aria-hidden="true">*</span></label>
      <input id="tf-kpi" v-model="kpi" type="text" required autocomplete="off" />
    </div>

    <div class="field">
      <label for="tf-url">Link <span class="req" aria-hidden="true">*</span></label>
      <div class="row">
        <input id="tf-url" v-model="url" type="text" inputmode="url" placeholder="https://" required autocomplete="off" :aria-invalid="urlTouched && url && !urlOk ? 'true' : 'false'" aria-describedby="tf-url-err" @blur="urlTouched = true" />
        <button type="button" class="btn secondary" data-test="test-link" :disabled="testing || !urlOk" @click="testLink">{{ testing ? 'Testing…' : 'Test link' }}</button>
      </div>
      <span id="tf-url-err" class="error-text small">{{ urlTouched && url && !urlOk ? 'Enter a link starting with http:// or https://' : '' }}</span>
    </div>

    <div class="field">
      <label for="tf-key">Key</label>
      <select id="tf-key" v-model="credentialId" data-test="key-select" :aria-invalid="keyProblem ? 'true' : 'false'" aria-describedby="tf-key-hint">
        <option :value="null">None</option>
        <option v-for="k in keys" :key="k.id" :value="k.id">{{ k.name }}</option>
      </select>
      <span id="tf-key-hint" class="hint" :class="keyProblem ? 'error-text' : 'muted'" :role="keyProblem ? 'alert' : undefined" data-test="key-problem">{{ keyProblem || 'Optional. Send a stored key (API token or login) with this link. Manage keys in Settings → Keys.' }}</span>
    </div>

    <div v-if="testError" class="error-text" role="alert">{{ testError }}</div>
    <div v-if="testResult" class="preview" data-test="test-result" role="status">
      <template v-if="testResult.status === 'blocked'">
        <strong>Blocked.</strong> {{ blockedWords }}
        <div v-if="testResult.error" class="muted small">{{ testResult.error }}</div>
      </template>
      <template v-else-if="testResult.status === 'key_rejected'">
        <strong class="error-text">The site rejected this key (HTTP {{ rejectedCode }}).</strong> Check the key or replace its secret.
      </template>
      <template v-else-if="testResult.status !== 'ok'">
        <strong class="error-text">Could not read this link.</strong>
        <div class="small">{{ testResult.error }}</div>
      </template>
      <template v-else>
        <strong>Looks good.</strong> <span class="muted">Read as {{ testResult.kind === 'api' ? 'an API (JSON)' : 'a web page' }}.</span>
        <pre v-if="testResult.text_preview !== undefined" tabindex="0" aria-label="Text preview">{{ testResult.text_preview }}</pre>
        <div v-if="jsonRows.length" class="table-wrap" tabindex="0" aria-label="JSON preview">
          <table>
            <thead><tr><th scope="col">Path</th><th scope="col">Value</th></tr></thead>
            <tbody><tr v-for="r in jsonRows" :key="r.path"><td class="mono">{{ r.path }}</td><td>{{ r.value }}</td></tr></tbody>
          </table>
        </div>
      </template>
    </div>

    <details class="adv" :open="advanced || isLogin" @toggle="advanced = ($event.target as HTMLDetailsElement).open">
      <summary>Advanced</summary>
      <div class="adv-body">
        <div class="field">
          <label for="tf-selector">Focus area (CSS selector)</label>
          <input id="tf-selector" v-model="selector" type="text" class="mono" placeholder="e.g. #pricing .price" autocomplete="off" />
          <span class="hint muted">Only watch this part of the page. Helps when the rest of the page changes all the time.</span>
        </div>
        <div class="field">
          <label for="tf-kind">Kind</label>
          <select id="tf-kind" v-model="kind">
            <option value="auto">Auto-detect</option>
            <option value="page">Web page</option>
            <option value="api">API (JSON)</option>
          </select>
        </div>
        <label class="check"><input v-model="useBrowser" type="checkbox" data-test="browser-box" :disabled="isLogin" aria-describedby="tf-browser-hint" /> Needs a browser (page builds itself with JavaScript)</label>
        <span v-if="isLogin" id="tf-browser-hint" class="hint muted">Logins always use the browser</span>
      </div>
    </details>

    <p v-if="saveError" class="error-text" role="alert">{{ saveError }}</p>
    <p v-if="!editing" class="muted small">New KPIs are checked silently the first time — you'll be alerted from the second change on.</p>
    <div class="toolbar end">
      <button type="button" class="btn secondary" @click="emit('cancel')">Cancel</button>
      <button type="submit" class="btn" :disabled="!valid || saving">{{ saving ? 'Saving…' : editing ? 'Save changes' : 'Add KPI' }}</button>
    </div>
  </form>
</template>

<style scoped>
.form { display: flex; flex-direction: column; gap: 12px; }
.req { color: var(--status-fail); }
.row { display: flex; gap: 8px; flex-wrap: wrap; }
.row input { flex: 1 1 14rem; min-width: 0; }
.hint, .small { font-size: 0.8125rem; }
.preview { border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface-2); padding: 10px 12px; display: flex; flex-direction: column; gap: 6px; min-width: 0; }
pre { margin: 0; max-height: 11rem; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 8px; font-family: var(--font-mono); font-size: 0.8125rem; }
.table-wrap { overflow: auto; max-height: 12rem; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); }
table { width: 100%; border-collapse: collapse; font-size: 0.8125rem; }
th, td { padding: 4px 8px; border-bottom: 1px solid var(--border); text-align: left; overflow-wrap: anywhere; }
.adv summary { cursor: pointer; font-weight: 600; font-size: 0.875rem; color: var(--link); }
.adv-body { display: flex; flex-direction: column; gap: 12px; padding-top: 10px; }
.check { display: flex; gap: 8px; align-items: flex-start; font-weight: 500; }
.check input { margin-top: 3px; accent-color: var(--accent); }
.end { justify-content: flex-end; }
</style>
