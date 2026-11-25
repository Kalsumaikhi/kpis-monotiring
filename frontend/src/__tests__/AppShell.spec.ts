import { describe, expect, it } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import AppShell from '../components/AppShell.vue'
import router from '../router'
import { setPassword } from '../api/client'

describe('AppShell drawer', () => {
  it('moves focus in on open, Escape closes and returns focus to the menu button', async () => {
    setPassword('p')
    await router.replace('/kpis')
    const w = mount(AppShell, { global: { plugins: [router] }, attachTo: document.body })
    const menu = w.get('.menu-btn')
    await menu.trigger('click')
    await flushPromises()
    expect(w.get('#sidebar').classes()).toContain('open')
    expect(document.activeElement).toBe(w.get('#sidebar nav a').element)

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await flushPromises()
    expect(w.get('#sidebar').classes()).not.toContain('open')
    expect(document.activeElement).toBe(menu.element)
    w.unmount()
  })

  it('has a close button inside the drawer', async () => {
    const w = mount(AppShell, { global: { plugins: [router] }, attachTo: document.body })
    await w.get('.menu-btn').trigger('click')
    await w.get('#sidebar .close-btn').trigger('click')
    expect(w.get('#sidebar').classes()).not.toContain('open')
    w.unmount()
  })
})
