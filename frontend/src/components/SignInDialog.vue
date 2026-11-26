<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ApiError, apiGet, apiSend } from '../api/client'
import type { LoginAttempt } from '../api/types'

// Interactive sign-in: starts an attempt, polls it every 2 s and asks for the emailed code.
// The code lives only in `code` (and the input) until it is sent; it is never stored or logged.
const props = defineProps<{ open: boolean; credentialId: number; keyName: string; host: string }>()
const emit = defineEmits<{ close: []; done: [] }>()

const CODE_RE = /^[A-Za-z0-9-]{1,64}$/
const BAD_CODE = 'the code can only contain letters, digits and dashes'
const EXPIRED = 'this sign-in expired — click Sign in now again'
const FINAL = ['succeeded', 'failed', 'expired', 'cancelled']
const ACTIVE = ['pending', 'signing_in', 'waiting_code', 'verifying']

const attempt = ref<LoginAttempt | null>(null)
const startError = ref('')
const starting = ref(false)
const submitted = ref(false) // the code was sent; waiting for the worker to pick it up
const cancelling = ref(false)
const codeError = ref('')
const code = ref('')
const codeEl = ref<HTMLInputElement | null>(null)
const now = ref(performance.now())
// Local deadline (monotonic clock) set from the server's remaining_seconds on every poll.
const deadline = ref<number | null>(null)
const dialog = ref<HTMLElement | null>(null)
let startSeq = 0 // a superseded start() is dropped
let pollSeq = 0 // stale poll responses are dropped
let inflight = 0 // pollSeq of the poll in flight (0 = none): ticks never overlap
let pollTimer: ReturnType<typeof setInterval> | null = null
let tickTimer: ReturnType<typeof setInterval> | null = null
let opener: HTMLElement | null = null
let doneSent = false

const status = computed(() => attempt.value?.status ?? null)
// The countdown follows the server's clock (remaining_seconds), not this device's: at 0 the code step
// is over even if the server has not flipped yet (polling goes on, so the server status wins).
const remaining = computed(() => (deadline.value === null ? null : Math.max(0, Math.ceil((deadline.value - now.value) / 1000))))
const clock = computed(() => {
  const r = remaining.value ?? 0
  return `${String(Math.floor(r / 60)).padStart(2, '0')}:${String(r % 60).padStart(2, '0')}`
})
const expiredLocally = computed(() => status.value === 'waiting_code' && remaining.value === 0)
type View = 'starting' | 'signing' | 'code' | 'verifying' | 'succeeded' | 'problem'
const view = computed<View>(() => {
  if (startError.value) return 'problem'
  const st = status.value
  if (st === null) return 'starting'
  if (st === 'succeeded') return 'succeeded'
  if (st === 'failed' || st === 'expired' || st === 'cancelled' || expiredLocally.value) return 'problem'
  if (st === 'verifying' || (st === 'waiting_code' && submitted.value)) return 'verifying'
  if (st === 'waiting_code') return 'code'
  return 'signing'
})
const problemText = computed(() => {
  if (startError.value) return startError.value
  if (status.value === 'expired' || expiredLocally.value) return attempt.value?.error || EXPIRED
  if (status.value === 'cancelled') return 'This sign-in was cancelled.'
  return attempt.value?.error || 'The sign-in failed.'
})
const trimmed = computed(() => code.value.trim())
const codeOk = computed(() => CODE_RE.test(trimmed.value))

function clearCode() {
  code.value = ''
  if (codeEl.value) codeEl.value.value = ''
}

function stop() {
  if (pollTimer) clearInterval(pollTimer)
  if (tickTimer) clearInterval(tickTimer)
  pollTimer = tickTimer = null
}

async function poll() {
  if (inflight) return
  const mine = ++pollSeq
  inflight = mine
  try {
    const a = await apiGet<LoginAttempt>(`/api/logins/${props.credentialId}/attempt`)
    if (mine !== pollSeq || !props.open) return
    // Never go back to an older attempt than the one being shown.
    if (attempt.value && a.attempt_id < attempt.value.attempt_id) return
    apply(a)
  } catch {
    /* transient: the next tick tries again */
  } finally {
    if (inflight === mine) inflight = 0
  }
}

function apply(a: LoginAttempt) {
  const prev = attempt.value
  // A new code step (rejected code, new waiting_since) or a new error means the code box is open again.
  if (prev && (prev.status !== a.status || prev.waiting_since !== a.waiting_since || prev.error !== a.error)) submitted.value = false
  attempt.value = a
  if (a.status === 'waiting_code' && a.remaining_seconds !== null && a.remaining_seconds !== undefined) {
    now.value = performance.now()
    deadline.value = now.value + a.remaining_seconds * 1000
  } else if (a.status !== 'waiting_code') {
    deadline.value = null
  }
  if (a.status === 'succeeded' && !doneSent) {
    doneSent = true
    emit('done')
  }
  if (FINAL.includes(a.status)) stop()
}

function startTimers() {
  stop()
  now.value = performance.now()
  pollTimer = setInterval(() => { if (!attempt.value || !FINAL.includes(attempt.value.status)) void poll() }, 2000)
  tickTimer = setInterval(() => { now.value = performance.now() }, 1000)
}

async function start() {
  const mine = ++startSeq
  pollSeq++ // drop anything still in flight from an earlier attempt
  inflight = 0
  stop()
  starting.value = true
  startError.value = ''
  attempt.value = null
  deadline.value = null
  submitted.value = false
  cancelling.value = false
  codeError.value = ''
  doneSent = false
  clearCode()
  try {
    const res = await apiSend<{ attempt_id: number; status: LoginAttempt['status'] }>('POST', `/api/logins/${props.credentialId}/sign-in`)
    if (mine !== startSeq) return
    attempt.value = { attempt_id: res.attempt_id, status: res.status, error: null, waiting_since: null, expires_at: null, remaining_seconds: null }
    startTimers()
    void poll()
  } catch (e) {
    if (mine !== startSeq) return
    if (e instanceof ApiError && e.status === 409) {
      await poll() // one is already running: follow it
      if (mine !== startSeq) return
      if (!attempt.value) startError.value = e.detail
      else if (!FINAL.includes(attempt.value.status)) startTimers() // a finished one needs no polling
    } else {
      startError.value = e instanceof ApiError ? e.detail : 'Could not start the sign-in'
      stop()
    }
  } finally {
    if (mine === startSeq) starting.value = false
  }
}

async function submitCode() {
  if (!attempt.value || submitted.value) return
  if (!codeOk.value) {
    codeError.value = BAD_CODE
    return
  }
  const typed = trimmed.value
  const id = attempt.value.attempt_id
  clearCode() // gone from state and DOM before the request leaves
  codeError.value = ''
  submitted.value = true
  try {
    await apiSend('POST', `/api/logins/attempts/${id}/code`, { code: typed })
  } catch (e) {
    submitted.value = false
    if (e instanceof ApiError && e.status === 410) {
      attempt.value = { ...attempt.value!, status: 'expired', error: e.detail }
      stop()
    } else {
      codeError.value = e instanceof ApiError ? e.detail : 'Could not send the code'
      if (e instanceof ApiError && e.status === 409) void poll()
    }
  }
}

async function cancelAttempt() {
  const a = attempt.value
  if (a && ACTIVE.includes(a.status)) {
    try { await apiSend('POST', `/api/logins/attempts/${a.attempt_id}/cancel`) } catch { /* already finished */ }
  }
}

async function cancel() {
  if (cancelling.value) return
  const a = attempt.value
  if (a && ACTIVE.includes(a.status) && !expiredLocally.value) {
    cancelling.value = true
    clearCode()
    await cancelAttempt()
    cancelling.value = false
  }
  emit('close')
}

async function tryAgain() {
  if (attempt.value && ACTIVE.includes(attempt.value.status)) await cancelAttempt() // expired here, not yet there
  await start()
}

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), [tabindex]:not([tabindex="-1"])'
function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.preventDefault()
    e.stopImmediatePropagation()
    void cancel()
    return
  }
  if (e.key !== 'Tab' || !dialog.value) return
  const items = Array.from(dialog.value.querySelectorAll<HTMLElement>(FOCUSABLE))
  if (!items.length) return
  const first = items[0]
  const last = items[items.length - 1]
  const active = document.activeElement as HTMLElement | null
  if (!active || !dialog.value.contains(active) || (e.shiftKey && active === first)) {
    e.preventDefault()
    ;(e.shiftKey && active === first ? last : first).focus()
  } else if (!e.shiftKey && active === last) {
    e.preventDefault()
    first.focus()
  }
}

function teardown() {
  startSeq++
  pollSeq++
  inflight = 0
  stop()
  clearCode()
  document.removeEventListener('keydown', onKey, true)
  if (opener !== null || props.open) {
    const target = opener && opener.isConnected ? opener : document.querySelector<HTMLElement>('main')
    target?.focus()
  }
  opener = null
}

watch(
  () => props.open,
  async (o) => {
    if (o) {
      opener = document.activeElement as HTMLElement | null
      document.addEventListener('keydown', onKey, true)
      void start()
      await nextTick()
      dialog.value?.querySelector<HTMLElement>('[data-test=cancel]')?.focus()
    } else {
      teardown()
    }
  },
  { immediate: true },
)
// Focus the code box as soon as it appears.
watch(view, async (v) => {
  if (v !== 'code') return
  await nextTick()
  codeEl.value?.focus()
})
// A parent that removes the dialog with v-if never flips `open`, so restore focus here too.
onBeforeUnmount(teardown)

// A click outside must not throw away a code step the user is in the middle of.
function onBackdrop() {
  if (status.value === 'waiting_code') return
  void cancel()
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="overlay" @click.self="onBackdrop">
      <div ref="dialog" class="dialog" role="dialog" aria-modal="true" aria-labelledby="si-title" aria-describedby="si-body">
        <h2 id="si-title">Sign in to {{ keyName }}</h2>
        <div id="si-body" class="body" aria-live="polite">
          <template v-if="cancelling">
            <p class="line"><span class="spinner" aria-hidden="true" />Cancelling… the browser finishes its current step</p>
            <p class="muted">If the sign-in was already accepted it will be discarded.</p>
          </template>
          <p v-else-if="view === 'starting' || view === 'signing'" class="line" data-test="signing"><span class="spinner" aria-hidden="true" />Signing in to {{ host }}…</p>
          <p v-else-if="view === 'verifying'" class="line" data-test="verifying"><span class="spinner" aria-hidden="true" />Checking the code…</p>
          <p v-else-if="view === 'succeeded'" class="line ok" data-test="succeeded"><span aria-hidden="true">✓</span> Signed in. Checking your KPIs now.</p>
          <p v-else-if="view === 'problem'" class="error-text" role="alert" data-test="problem">{{ problemText }}</p>
        </div>

        <form v-if="view === 'code' && !cancelling" class="code" novalidate autocomplete="off" @submit.prevent="submitCode">
          <p id="si-wait">We sent a code to your email. Enter it within <strong class="clock" data-test="countdown">{{ clock }}</strong>.</p>
          <div class="field">
            <label for="si-code">Code</label>
            <input
              id="si-code" ref="codeEl" v-model="code" type="text" inputmode="numeric" autocomplete="one-time-code" maxlength="64"
              autocapitalize="off" spellcheck="false" class="mono" aria-describedby="si-wait si-code-err" :aria-invalid="codeError ? 'true' : 'false'"
            />
            <span id="si-code-err" class="error-text small" role="alert">{{ codeError }}</span>
          </div>
          <div class="actions">
            <button type="button" class="btn secondary" data-test="cancel" @click="cancel">Cancel</button>
            <button type="submit" class="btn" data-test="code-submit" :disabled="!trimmed">Submit</button>
          </div>
        </form>
        <div v-else class="actions">
          <button v-if="view === 'problem'" type="button" class="btn" data-test="try-again" :disabled="starting" @click="tryAgain">Try again</button>
          <button v-if="view === 'succeeded' || view === 'problem'" type="button" class="btn secondary" data-test="close" @click="emit('close')">Close</button>
          <button v-else type="button" class="btn secondary" data-test="cancel" :disabled="cancelling" @click="cancel">Cancel</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.overlay { position: fixed; inset: 0; background: var(--overlay); display: flex; align-items: center; justify-content: center; padding: 16px; z-index: 50; }
.dialog { background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px; width: min(440px, 100%); max-height: 100%; overflow: auto; display: flex; flex-direction: column; gap: 12px; }
.body p { margin: 0; }
.code { display: flex; flex-direction: column; gap: 12px; }
.code p { margin: 0; }
.clock { font-variant-numeric: tabular-nums; }
.line { display: flex; align-items: center; gap: 8px; }
.line.ok { color: var(--status-ok); font-weight: 600; }
.spinner { width: 14px; height: 14px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 1s linear infinite; flex: none; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spinner { animation-duration: 3s; } }
.small { font-size: 0.8125rem; }
.actions { display: flex; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }
</style>
