<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { ApiError, apiSend } from '../../api/client'
import type { KeyIn, KeyKind, KeyPatch, StoredKey } from '../../api/types'

// mode "replace" shows only the secret field (PATCH {secret}); "edit" leaves the secret blank to keep it.
const props = defineProps<{ keyItem?: StoredKey | null; replace?: boolean; unavailable?: boolean }>()
const emit = defineEmits<{ saved: []; cancel: []; unavailable: [] }>()

const editing = computed(() => !!props.keyItem)
const name = ref(props.keyItem?.name ?? '')
const kind = ref<KeyKind>(props.keyItem?.kind ?? 'header')
const headerName = ref(props.keyItem?.header_name ?? 'Authorization')
const prefix = ref(props.keyItem?.prefix ?? '')
const paramName = ref(props.keyItem?.param_name ?? '')
const username = ref(props.keyItem?.username ?? '')
// Write-only: lives only in this ref, is cleared right after the request, and is never logged or stored.
const secret = ref('')
const showSecret = ref(false)
const secretEl = ref<HTMLInputElement | null>(null)
// Clear the DOM value as well: a parent that unmounts this form right away never re-renders the bound value.
function clearSecret() {
  secret.value = ''
  if (secretEl.value) secretEl.value.value = ''
}
const saving = ref(false)
const error = ref('')
const first = ref<HTMLElement | null>(null)

const secretRequired = computed(() => !editing.value || !!props.replace)
const kindOk = computed(() => {
  if (kind.value === 'query') return paramName.value.trim() !== ''
  if (kind.value === 'basic') return username.value.trim() !== ''
  return true
})
const valid = computed(() => !(props.unavailable && (secretRequired.value || secret.value !== '')) && (props.replace ? true : name.value.trim() !== '' && kindOk.value) && (!secretRequired.value || secret.value !== ''))

onBeforeUnmount(clearSecret)
function cancel() {
  clearSecret()
  showSecret.value = false
  emit('cancel')
}

onMounted(async () => {
  await nextTick()
  first.value?.focus()
})

function fields(): Partial<KeyIn> {
  return {
    header_name: kind.value === 'header' ? headerName.value.trim() || null : null,
    prefix: kind.value === 'header' ? prefix.value.trim() || null : null,
    param_name: kind.value === 'query' ? paramName.value.trim() : null,
    username: kind.value === 'basic' ? username.value.trim() : null,
  }
}

async function save() {
  if (!valid.value || saving.value) return
  saving.value = true
  error.value = ''
  const typed = secret.value
  try {
    if (props.keyItem && props.replace) {
      const body: KeyPatch = { secret: typed }
      await apiSend('PATCH', `/api/credentials/${props.keyItem.id}`, body)
    } else if (props.keyItem) {
      const body: KeyPatch = { name: name.value.trim(), kind: kind.value, ...fields() }
      if (typed !== '') body.secret = typed // blank keeps the stored secret
      await apiSend('PATCH', `/api/credentials/${props.keyItem.id}`, body)
    } else {
      const body: KeyIn = { name: name.value.trim(), kind: kind.value, ...fields(), secret: typed }
      await apiSend('POST', '/api/credentials', body)
    }
    clearSecret()
    emit('saved')
  } catch (e) {
    if (e instanceof ApiError && e.status === 503) emit('unavailable')
    error.value = e instanceof ApiError ? e.detail : 'Could not save'
  } finally {
    clearSecret()
    showSecret.value = false
    saving.value = false
  }
}

const title = computed(() => (props.replace ? `Replace secret for ${props.keyItem?.name}` : editing.value ? 'Edit key' : 'Add key'))
</script>

<template>
  <form class="card form" novalidate :aria-label="title" autocomplete="off" @submit.prevent="save">
    <h3>{{ title }}</h3>
    <p class="muted small"><span aria-hidden="true">*</span> Required. The secret is stored encrypted and can't be shown again.</p>

    <template v-if="!replace">
      <div class="field">
        <label for="kf-name">Name <span class="req" aria-hidden="true">*</span></label>
        <input id="kf-name" :ref="(el) => (first = el as HTMLElement)" v-model="name" type="text" required autocomplete="off" placeholder="e.g. Acme API token" />
      </div>

      <fieldset class="field kinds">
        <legend>Type</legend>
        <label class="radio"><input v-model="kind" type="radio" name="kf-kind" value="header" /> Header (token or API key)</label>
        <label class="radio"><input v-model="kind" type="radio" name="kf-kind" value="query" /> Link parameter</label>
        <label class="radio"><input v-model="kind" type="radio" name="kf-kind" value="basic" /> Username and password</label>
      </fieldset>

      <template v-if="kind === 'header'">
        <div class="field">
          <label for="kf-header">Header name</label>
          <input id="kf-header" v-model="headerName" type="text" class="mono" autocomplete="off" placeholder="Authorization" />
        </div>
        <div class="field">
          <label for="kf-prefix">Prefix</label>
          <input id="kf-prefix" v-model="prefix" type="text" class="mono" autocomplete="off" placeholder="e.g. Bearer" />
          <span class="hint muted">Optional. Sent as “prefix secret”, for example “Bearer abc123”.</span>
        </div>
      </template>
      <div v-else-if="kind === 'query'" class="field">
        <label for="kf-param">Parameter name <span class="req" aria-hidden="true">*</span></label>
        <input id="kf-param" v-model="paramName" type="text" class="mono" required autocomplete="off" placeholder="e.g. api_key" />
        <span class="hint muted">Added to the link as ?name=secret when it is fetched.</span>
      </div>
      <div v-else class="field">
        <label for="kf-user">Username <span class="req" aria-hidden="true">*</span></label>
        <input id="kf-user" v-model="username" type="text" required autocomplete="off" />
      </div>
    </template>

    <div class="field">
      <label for="kf-secret">
        {{ kind === 'basic' ? 'Password' : 'Secret' }}
        <span v-if="secretRequired" class="req" aria-hidden="true">*</span>
        <span v-else class="muted"> — Leave blank to keep the current secret</span>
      </label>
      <div class="secret">
        <input
          id="kf-secret" :ref="(el) => { secretEl = el as HTMLInputElement | null; if (replace) first = el as HTMLElement }" v-model="secret" :type="showSecret ? 'text' : 'password'"
          class="mono" autocomplete="new-password" autocapitalize="off" spellcheck="false" :required="secretRequired" :disabled="unavailable" data-test="secret"
        />
        <button type="button" class="btn secondary" aria-label="Show secret" aria-controls="kf-secret" :aria-pressed="showSecret" data-test="toggle-secret" @click="showSecret = !showSecret">{{ showSecret ? 'Hide' : 'Show' }}</button>
      </div>
    </div>

    <p v-if="unavailable" class="muted small">Secrets can't be saved until KPI_SECRET_KEY is set on the server.</p>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p>
    <div class="toolbar end">
      <button type="button" class="btn secondary" data-test="cancel" @click="cancel">Cancel</button>
      <button type="submit" class="btn" :disabled="!valid || saving">{{ saving ? 'Saving…' : replace ? 'Replace secret' : editing ? 'Save changes' : 'Add key' }}</button>
    </div>
  </form>
</template>

<style scoped>
.form { display: flex; flex-direction: column; gap: 12px; }
.req { color: var(--status-fail); }
.small, .hint { font-size: 0.8125rem; }
.kinds { border: 0; padding: 0; margin: 0; gap: 4px; min-width: 0; }
.kinds legend { font-weight: 600; padding: 0; margin-bottom: 4px; }
.radio { display: flex; gap: 8px; align-items: center; font-weight: 500; min-height: 32px; }
.radio input { accent-color: var(--accent); }
.secret { display: flex; gap: 8px; }
.secret input { flex: 1 1 auto; min-width: 0; }
.end { justify-content: flex-end; }
</style>
