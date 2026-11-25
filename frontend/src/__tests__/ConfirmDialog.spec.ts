import { describe, expect, it } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, ref } from 'vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'

const Host = defineComponent({
  setup(_, { expose }) {
    const open = ref(false)
    const cancelled = ref(0)
    expose({ open, cancelled })
    return () =>
      h('div', [
        h('button', { id: 'opener', onClick: () => (open.value = true) }, 'Open'),
        h(ConfirmDialog, { open: open.value, title: 'Reject run?', onCancel: () => { cancelled.value++; open.value = false }, onConfirm: () => (open.value = false) }),
      ])
  },
})

const press = (key: string, shiftKey = false) => document.dispatchEvent(new KeyboardEvent('keydown', { key, shiftKey, bubbles: true, cancelable: true }))

describe('ConfirmDialog', () => {
  it('focuses cancel, traps Tab, closes on Escape and restores focus to the opener', async () => {
    const w = mount(Host, { attachTo: document.body })
    const opener = w.get('#opener').element as HTMLElement
    opener.focus()
    await w.get('#opener').trigger('click')
    await flushPromises()

    const dlg = document.body.querySelector('[role="dialog"]') as HTMLElement
    expect(dlg.getAttribute('aria-modal')).toBe('true')
    expect(dlg.getAttribute('aria-labelledby')).toBe('cd-title')
    const [cancel, confirm] = Array.from(dlg.querySelectorAll('button'))
    expect(document.activeElement).toBe(cancel)

    confirm.focus()
    press('Tab')
    expect(document.activeElement).toBe(cancel)
    press('Tab', true)
    expect(document.activeElement).toBe(confirm)

    press('Escape')
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(document.activeElement).toBe(opener)
    expect((w.vm as unknown as { cancelled: number }).cancelled).toBe(1)
    w.unmount()
  })
})

describe('ConfirmDialog mounted open', () => {
  it('focuses Cancel and Escape emits cancel (and prevents default so the drawer stays put)', async () => {
    const w = mount(ConfirmDialog, { props: { open: true, title: 'Sure?' }, attachTo: document.body })
    await flushPromises()
    const cancel = document.body.querySelector('[role="dialog"] button') as HTMLElement
    expect(document.activeElement).toBe(cancel)
    const ev = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true, bubbles: true })
    document.dispatchEvent(ev)
    expect(ev.defaultPrevented).toBe(true)
    expect(w.emitted('cancel')).toHaveLength(1)
    w.unmount()
  })
})
