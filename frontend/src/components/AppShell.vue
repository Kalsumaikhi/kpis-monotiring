<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { clearPassword } from '../api/auth'
import { setTheme, theme, type ThemeChoice } from '../composables/useTheme'

const route = useRoute()
const router = useRouter()
const menuOpen = ref(false)
const menuBtn = ref<HTMLButtonElement | null>(null)
const sidebar = ref<HTMLElement | null>(null)
watch(() => route.fullPath, () => (menuOpen.value = false))

// Drawer a11y: focus moves into it on open and back to the menu button on close.
watch(menuOpen, async (open, was) => {
  await nextTick()
  if (open) sidebar.value?.querySelector<HTMLElement>('nav a')?.focus()
  else if (was) menuBtn.value?.focus()
})
function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape' && menuOpen.value) menuOpen.value = false
}
onMounted(() => document.addEventListener('keydown', onKey))
onBeforeUnmount(() => document.removeEventListener('keydown', onKey))

const nav = [
  { to: '/', label: 'Overview', icon: 'M3 10.5L10 4l7 6.5V16a1 1 0 01-1 1h-3.5v-4.5h-5V17H4a1 1 0 01-1-1z' },
  { to: '/changes', label: 'Changes', icon: 'M4 6h12M4 10h12M4 14h7' },
  { to: '/explorer', label: 'Explorer', icon: 'M9 15a6 6 0 100-12 6 6 0 000 12zM13.5 13.5L17 17' },
  { to: '/kpis', label: 'KPI frequency', icon: 'M3 15l4-5 3 3 6-8M3 17h14' },
  { to: '/uploads', label: 'Uploads', icon: 'M10 14V4M6 8l4-4 4 4M4 16h12' },
  { to: '/settings', label: 'Settings', icon: 'M10 12.5a2.5 2.5 0 100-5 2.5 2.5 0 000 5zM10 3v2M10 15v2M3 10h2M15 10h2M5 5l1.4 1.4M13.6 13.6L15 15M15 5l-1.4 1.4M6.4 13.6L5 15' },
]

function isActive(to: string): boolean {
  return to === '/' ? route.path === '/' : route.path === to || route.path.startsWith(to + '/')
}

const themes: { value: ThemeChoice; label: string }[] = [
  { value: 'system', label: 'System' },
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
]

function signOut() {
  clearPassword()
  void router.replace('/login')
}
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <button ref="menuBtn" class="menu-btn" type="button" :aria-expanded="menuOpen" aria-controls="sidebar" @click="menuOpen = !menuOpen">
        <svg viewBox="0 0 20 20" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" aria-hidden="true"><path d="M3 5h14M3 10h14M3 15h14" /></svg>
        <span class="sr-only">Menu</span>
      </button>
      <span class="brand">KPI Monitor</span>
    </header>

    <aside id="sidebar" ref="sidebar" class="sidebar" :class="{ open: menuOpen }">
      <button class="close-btn" type="button" @click="menuOpen = false">
        <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" aria-hidden="true"><path d="M5 5l10 10M15 5L5 15" /></svg>
        <span>Close menu</span>
      </button>
      <div class="brand desktop-brand">
        <svg viewBox="0 0 32 32" width="26" height="26" aria-hidden="true"><path d="M5 23l7-8 5 4 10-12" fill="none" stroke="var(--nav-active)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" /></svg>
        <span>KPI Monitor</span>
      </div>
      <nav aria-label="Main">
        <RouterLink v-for="n in nav" :key="n.to" :to="n.to" class="nav-item" :class="{ active: isActive(n.to) }" :aria-current="isActive(n.to) ? 'page' : undefined">
          <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path :d="n.icon" /></svg>
          <span>{{ n.label }}</span>
        </RouterLink>
      </nav>
      <div class="foot">
        <div class="theme" role="group" aria-label="Theme">
          <button v-for="t in themes" :key="t.value" type="button" class="theme-btn" :class="{ on: theme === t.value }" :aria-pressed="theme === t.value" @click="setTheme(t.value)">{{ t.label }}</button>
        </div>
        <button type="button" class="signout" @click="signOut">Sign out</button>
      </div>
    </aside>
    <div v-if="menuOpen" class="scrim" @click="menuOpen = false" />

    <main class="content">
      <slot />
    </main>
  </div>
</template>

<style scoped>
.shell { min-height: 100vh; display: grid; grid-template-columns: 232px minmax(0, 1fr); }
.topbar { display: none; }
.sidebar {
  background: var(--nav-bg); color: var(--nav-text);
  position: sticky; top: 0; height: 100vh;
  display: flex; flex-direction: column; padding: 16px 12px; gap: 16px; overflow-y: auto;
}
.brand { display: flex; align-items: center; gap: 10px; font-weight: 700; font-size: 1.05rem; letter-spacing: -0.01em; }
.desktop-brand { color: #fff; padding: 4px 8px 8px; }
nav { display: flex; flex-direction: column; gap: 2px; }
.nav-item {
  display: flex; align-items: center; gap: 10px; padding: 9px 10px; border-radius: var(--radius);
  color: var(--nav-text); text-decoration: none; font-weight: 500; border-left: 3px solid transparent;
}
.nav-item:hover { background: var(--nav-hover); }
.nav-item.active { color: var(--nav-active); border-left-color: var(--nav-active); background: var(--nav-hover); font-weight: 650; }
.nav-item:focus-visible, .theme-btn:focus-visible, .signout:focus-visible, .menu-btn:focus-visible { outline: 2px solid var(--nav-active); outline-offset: 2px; }
.foot { margin-top: auto; display: flex; flex-direction: column; gap: 10px; }
.theme { display: grid; grid-template-columns: repeat(3, 1fr); border: 1px solid rgba(255, 255, 255, 0.22); border-radius: var(--radius); overflow: hidden; }
.theme-btn { background: transparent; border: 0; color: var(--nav-text); padding: 6px 4px; font: inherit; font-size: 0.8125rem; cursor: pointer; min-height: 32px; }
.theme-btn + .theme-btn { border-left: 1px solid rgba(255, 255, 255, 0.22); }
.theme-btn.on { background: var(--nav-active); color: var(--c-dark-green); font-weight: 650; }
.signout { background: transparent; border: 0; color: var(--nav-text); text-align: left; padding: 6px 10px; font: inherit; cursor: pointer; text-decoration: underline; text-underline-offset: 3px; }
.content { padding: 24px 28px 48px; min-width: 0; max-width: 1280px; width: 100%; }
.scrim { display: none; }
.close-btn { display: none; }

@media (max-width: 800px) {
  .shell { display: block; }
  .topbar { display: flex; align-items: center; gap: 12px; background: var(--nav-bg); color: #fff; padding: 8px 16px; position: sticky; top: 0; z-index: 20; }
  .menu-btn { background: transparent; border: 1px solid rgba(255, 255, 255, 0.3); color: #fff; width: 40px; height: 40px; border-radius: var(--radius); display: grid; place-items: center; cursor: pointer; }
  .sidebar { position: fixed; z-index: 30; left: 0; top: 0; bottom: 0; width: min(280px, 85vw); height: auto; transform: translateX(-100%); visibility: hidden; transition: transform 0.18s ease, visibility 0.18s; }
  .sidebar.open { transform: none; visibility: visible; }
  .desktop-brand { display: none; }
  .close-btn { display: inline-flex; align-items: center; gap: 8px; align-self: flex-start; background: transparent; border: 1px solid rgba(255, 255, 255, 0.3); color: #fff; border-radius: var(--radius); padding: 6px 10px; min-height: 36px; font: inherit; cursor: pointer; }
  .close-btn:focus-visible { outline: 2px solid var(--nav-active); outline-offset: 2px; }
  .scrim { display: block; position: fixed; inset: 0; background: var(--overlay); z-index: 25; }
  .content { padding: 16px 16px 40px; }
}
</style>
