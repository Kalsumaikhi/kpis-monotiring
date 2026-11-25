import { describe, expect, it } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { defineComponent, h, ref } from 'vue'
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
    await flushPromises()
    await w.get('.menu-btn').trigger('click')
    expect(w.get('#sidebar').classes()).toContain('open')
    await w.get('#sidebar .close-btn').trigger('click')
    expect(w.get('#sidebar').classes()).not.toContain('open')
    w.unmount()
  })

  it('Escape closes an open ConfirmDialog but leaves the drawer open', async () => {
    const open = ref(false)
    const Page = defineComponent({
      setup: () => () => h(ConfirmDialog, { open: open.value, title: 'Sure?', onCancel: () => (open.value = false) }),
    })
    const w = mount(AppShell, { slots: { default: () => h(Page) }, global: { plugins: [router] }, attachTo: document.body })
    await flushPromises() // let the router's initial navigation settle
    await w.get('.menu-btn').trigger('click')
    await flushPromises()
    open.value = true
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()
    expect(w.get('#sidebar').classes()).toContain('open')

    // a real key press targets the focused element and bubbles up to the document
    document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(w.get('#sidebar').classes()).toContain('open')
    w.unmount()
  })
})
