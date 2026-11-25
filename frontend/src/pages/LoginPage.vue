<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError, apiGet, clearPassword, setPassword } from '../api/client'
import { takeLoginMessage } from '../api/auth'
import { safeNext } from '../router/safeNext'
import type { Source } from '../api/types'

const route = useRoute()
const router = useRouter()
const password = ref('')
const busy = ref(false)
const error = ref<string | null>(takeLoginMessage())

const target = () => safeNext(route.query.next)

async function submit() {
  if (!password.value || busy.value) return
  busy.value = true
  error.value = null
  setPassword(password.value)
  try {
    await apiGet<Source[]>('/api/sources')
    await router.replace(target())
  } catch (e) {
    clearPassword()
    if (e instanceof ApiError && e.status === 401) {
      error.value = takeLoginMessage() ?? 'Password incorrect or expired'
    } else {
      error.value = e instanceof ApiError ? `The server answered: ${e.detail}` : 'Could not reach the server. Check your connection and try again.'
    }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="login">
    <section class="panel">
      <div class="mark" aria-hidden="true">
        <svg viewBox="0 0 32 32" width="30" height="30"><path d="M5 23l7-8 5 4 10-12" fill="none" stroke="var(--c-spring-green)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" /></svg>
      </div>
      <h1>KPI Monitor</h1>
      <p class="muted">Enter the dashboard password to see what changed in your data sources.</p>
      <form @submit.prevent="submit">
        <div class="field">
          <label for="pw">Password</label>
          <input id="pw" v-model="password" type="password" autocomplete="current-password" autofocus :aria-invalid="!!error" :aria-describedby="error ? 'pw-err' : undefined" />
        </div>
        <p v-if="error" id="pw-err" class="error-text" role="alert">{{ error }}</p>
        <button class="btn" type="submit" :disabled="busy || !password">{{ busy ? 'Checking…' : 'Sign in' }}</button>
      </form>
    </section>
  </main>
</template>

<style scoped>
.login { min-height: 100vh; display: grid; place-items: center; padding: 16px; background: var(--bg); }
.panel { width: min(400px, 100%); background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); padding: 28px 24px; display: flex; flex-direction: column; gap: 14px; overflow: hidden; }
.mark { width: 48px; height: 48px; border-radius: 10px; background: var(--c-dark-green); display: grid; place-items: center; }
form { display: flex; flex-direction: column; gap: 14px; }
input { width: 100%; }
</style>
