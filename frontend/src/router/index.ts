import { createRouter, createWebHistory } from 'vue-router'
import { hasPassword } from '../api/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('../pages/LoginPage.vue'), meta: { public: true, title: 'Sign in' } },
    { path: '/', name: 'overview', component: () => import('../pages/OverviewPage.vue'), meta: { title: 'Overview' } },
    { path: '/changes', name: 'changes', component: () => import('../pages/ChangeFeedPage.vue'), meta: { title: 'Changes' } },
    { path: '/runs/:id', name: 'run', component: () => import('../pages/RunDetailPage.vue'), meta: { title: 'Run' } },
    {
      path: '/explorer/:slug?/:indicator?/:country?',
      name: 'explorer',
      component: () => import('../pages/ExplorerPage.vue'),
      meta: { title: 'Explorer' },
    },
    { path: '/kpis', name: 'kpis', component: () => import('../pages/KpiFrequencyPage.vue'), meta: { title: 'KPI frequency' } },
    { path: '/uploads', name: 'uploads', component: () => import('../pages/UploadsPage.vue'), meta: { title: 'Uploads' } },
    { path: '/settings', name: 'settings', component: () => import('../pages/SettingsPage.vue'), meta: { title: 'Settings' } },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach((to) => {
  if (to.meta.public) {
    if (to.path === '/login' && hasPassword()) {
      const n = Array.isArray(to.query.next) ? to.query.next[0] : to.query.next
      return typeof n === 'string' && n.startsWith('/') && !n.startsWith('//') && !n.startsWith('/\\') && !n.startsWith('/login') ? n : '/'
    }
    return true
  }
  if (hasPassword()) return true
  return { path: '/login', query: { next: to.fullPath }, replace: true }
})

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} · KPI Monitor` : 'KPI Monitor'
})

export default router
