<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ApiError, apiGet, apiSend } from '../../api/client'
import type { KeyUsedBy, StoredKey } from '../../api/types'
import ConfirmDialog from '../../components/ConfirmDialog.vue'
import SignInDialog from '../../components/SignInDialog.vue'
import { useToast } from '../../composables/useToast'
import { absoluteTime, relativeTime } from '../../lib/format'
import KeyForm from './KeyForm.vue'

const { show } = useToast()
const keys = ref<StoredKey[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const unavailable = ref(false)
const form = ref<{ key: StoredKey | null; replace: boolean; id: number } | null>(null)
let formId = 0
const pendingDelete = ref<StoredKey | null>(null)
const inUse = ref<{ name: string; used: KeyUsedBy } | null>(null)
const deleting = ref(false)
const signingIn = ref<StoredKey | null>(null)
const LOGIN_BADGE: Record<string, { label: string; tone: string }> = {
  ok: { label: 'Signed in', tone: 'ok' },
  login_needed: { label: 'Login needed', tone: 'warn' },
  signing_in: { label: 'Signing in…', tone: 'muted' },
  waiting_code: { label: 'Signing in…', tone: 'muted' },
  failed: { label: 'Sign-in failed', tone: 'fail' },
}
const loginBadge = (k: StoredKey) => LOGIN_BADGE[k.login_status ?? 'login_needed'] ?? LOGIN_BADGE.login_needed
const signInBusy = (k: StoredKey) => k.login_status === 'signing_in' || k.login_status === 'waiting_code'
const hostOf = (u?: string | null) => { try { return new URL(u ?? '').hostname } catch { return u ?? '' } }

const NO_KEY_PANEL = "Keys are encrypted with KPI_SECRET_KEY, which isn't set. Ask whoever runs the server to set it (see README → Keys), then reload."

async function load() {
  loading.value = true
  error.value = null
  try {
    try {
      const st = await apiGet<{ configured?: boolean }>('/api/credentials/status')
      unavailable.value = st?.configured === false
    } catch {
      unavailable.value = false // unknown: neutral; the server still answers 503 on a write
    }
    const res = await apiGet<StoredKey[]>('/api/credentials')
    keys.value = Array.isArray(res) ? res : []
  } catch (e) {
    if (e instanceof ApiError && e.status === 503) unavailable.value = true
    else error.value = e instanceof ApiError ? e.detail : 'Could not load keys'
  } finally {
    loading.value = false
  }
}
onMounted(load)

function summary(k: StoredKey): string {
  if (k.kind === 'header') return `Header ${k.header_name || 'Authorization'}: ${k.prefix ? k.prefix + ' ' : ''}••••`
  if (k.kind === 'query') return `Link parameter ${k.param_name}=••••`
  if (k.kind === 'login') return `Browser login ${hostOf(k.login_url)} as ${k.username}`
  return `Username ${k.username} + password`
}
const usedCount = (k: StoredKey) => k.used_by.targets.length + k.used_by.sources.length
const hasForm = computed(() => form.value !== null)

function open(key: StoredKey | null, replace = false) {
  form.value = { key, replace, id: ++formId }
}
async function onSaved() {
  const replaced = form.value?.replace
  form.value = null
  show(replaced ? 'Secret replaced' : 'Key saved')
  await load()
}

function askDelete(k: StoredKey) {
  inUse.value = null
  pendingDelete.value = k
}
async function doDelete() {
  const k = pendingDelete.value
  if (!k || deleting.value) return
  deleting.value = true
  try {
    await apiSend('DELETE', `/api/credentials/${k.id}`)
    pendingDelete.value = null
    show(`Key “${k.name}” deleted`)
    await load()
  } catch (e) {
    pendingDelete.value = null
    if (e instanceof ApiError && e.status === 409) {
      const used = (e.body as { used_by?: KeyUsedBy } | null)?.used_by ?? k.used_by
      inUse.value = { name: k.name, used: { targets: used.targets ?? [], sources: used.sources ?? [] } }
    } else {
      show(e instanceof ApiError ? e.detail : 'Could not delete the key', 'error')
    }
  } finally {
    deleting.value = false
  }
}
</script>

<template>
  <section id="logins" class="card" aria-labelledby="keys-h">
    <div class="head">
      <h2 id="keys-h">Keys</h2>
      <button type="button" class="btn" data-test="add-key" :disabled="unavailable || hasForm" @click="open(null)">Add key</button>
    </div>
    <p class="muted intro">Store an API token or login once, then pick it by name on a KPI link or a built-in source. Secrets are encrypted, sent only over https, and never shown again.</p>

    <div v-if="unavailable" class="setup" role="alert" data-test="setup-panel">
      <strong>Keys can't be saved yet.</strong>
      <span>{{ NO_KEY_PANEL }}</span>
    </div>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p>

    <div v-if="inUse" class="inuse" role="alert" data-test="in-use">
      <strong>“{{ inUse.name }}” is in use, so it can't be deleted.</strong>
      <span>Remove it from these first:</span>
      <ul>
        <li v-for="t in inUse.used.targets" :key="`t${t.id}`">KPI {{ t.kpi_name }} <span class="muted">({{ t.source }})</span></li>
        <li v-for="s in inUse.used.sources" :key="`s${s}`">Source {{ s }}</li>
      </ul>
      <button type="button" class="btn secondary" @click="inUse = null">Dismiss</button>
    </div>

    <KeyForm v-if="form" :key="form.id" :key-item="form.key" :replace="form.replace" :unavailable="unavailable" @saved="onSaved" @cancel="form = null" @unavailable="unavailable = true" />

    <p v-if="loading && !keys.length" class="muted state">Loading…</p>
    <p v-else-if="!keys.length && !error" class="muted state">No keys yet. Add one to watch links that need a login or API token.</p>
    <ul v-else class="list">
      <li v-for="k in keys" :key="k.id" class="item" :data-test="`key-${k.id}`">
        <div class="main">
          <strong class="kname">{{ k.name }}</strong>
          <span class="mono sum">{{ summary(k) }}</span>
          <div class="meta">
            <span v-if="k.readable && k.has_secret" class="state-ok"><span aria-hidden="true">✓</span> Set</span>
            <span v-else class="state-warn" data-test="needs-reentry"><span aria-hidden="true">!</span> Needs re-entry</span>
            <span class="muted" :title="absoluteTime(k.last_used_at)">{{ k.last_used_at ? `Last used ${relativeTime(k.last_used_at)}` : 'Never used' }}</span>
            <span class="muted">Used by {{ usedCount(k) }}</span>
          </div>
          <div v-if="k.kind === 'login'" class="meta" data-test="login-state">
            <span class="lbadge" :class="`tone-${loginBadge(k).tone}`"><span aria-hidden="true">{{ loginBadge(k).tone === 'ok' ? '✓' : loginBadge(k).tone === 'fail' ? '✕' : loginBadge(k).tone === 'warn' ? '!' : '…' }}</span> {{ loginBadge(k).label }}</span>
            <span v-if="k.login_error" class="error-text">{{ k.login_error }}</span>
          </div>
          <p v-if="k.last_error" class="error-text small">{{ k.last_error }}</p>
        </div>
        <div class="actions">
          <button v-if="k.kind === 'login'" type="button" class="btn" :data-test="`signin-${k.id}`" :disabled="unavailable || signInBusy(k) || !k.readable || !k.has_secret" @click="signingIn = k">Sign in now</button>
          <button type="button" class="btn" :class="{ secondary: k.readable }" :data-test="`replace-${k.id}`" :disabled="unavailable" @click="open(k, true)">Replace secret</button>
          <button type="button" class="btn secondary" :data-test="`edit-${k.id}`" @click="open(k)">Edit</button>
          <button type="button" class="btn danger" :data-test="`delete-${k.id}`" @click="askDelete(k)">Delete</button>
        </div>
      </li>
    </ul>

    <SignInDialog
      v-if="signingIn" :open="true" :credential-id="signingIn.id" :key-name="signingIn.name" :host="hostOf(signingIn.login_url)"
      @close="signingIn = null; load()" @done="load"
    />
    <ConfirmDialog
      :open="pendingDelete !== null" :title="`Delete key “${pendingDelete?.name ?? ''}”?`"
      message="Keys that are in use can't be deleted — remove them from those KPIs and sources first." confirm-label="Delete key" danger
      @confirm="doDelete" @cancel="pendingDelete = null"
    />
  </section>
</template>

<style scoped>
.head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
.intro { margin: 4px 0 12px; }
.state { text-align: center; padding: 20px; }
.setup, .inuse { display: flex; flex-direction: column; gap: 6px; align-items: flex-start; border: 1px solid var(--status-warn); border-radius: var(--radius); padding: 10px 12px; margin-bottom: 12px; background: var(--surface-2); }
.inuse { border-color: var(--status-fail); }
.inuse ul { margin: 0; padding-left: 1.2rem; }
.list { list-style: none; margin: 12px 0 0; padding: 0; border: 1px solid var(--border); border-radius: var(--radius); }
.item { display: flex; gap: 12px 16px; justify-content: space-between; flex-wrap: wrap; padding: 12px; border-bottom: 1px solid var(--border); }
.item:last-child { border-bottom: 0; }
.main { display: flex; flex-direction: column; gap: 4px; min-width: 0; flex: 1 1 18rem; }
.kname { overflow-wrap: anywhere; }
.sum { font-size: 0.8125rem; color: var(--link); overflow-wrap: anywhere; }
.meta { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 0.8125rem; align-items: center; }
.state-ok { color: var(--status-ok); font-weight: 600; }
.state-warn { color: var(--status-warn); font-weight: 600; border: 1px solid currentColor; border-radius: 999px; padding: 0 8px; }
.lbadge { display: inline-flex; gap: 5px; font-weight: 600; border: 1px solid currentColor; border-radius: 999px; padding: 0 8px; }
.tone-ok { color: var(--status-ok); }
.tone-warn { color: var(--status-warn); }
.tone-fail { color: var(--status-fail); }
.tone-muted { color: var(--text-muted); }
.small { font-size: 0.8125rem; margin: 0; overflow-wrap: anywhere; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: flex-start; }
</style>
