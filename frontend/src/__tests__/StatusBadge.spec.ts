import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import StatusBadge from '../components/StatusBadge.vue'

const labels: Record<string, string> = {
  ok: 'OK', baseline: 'Baseline', no_change: 'No change', failed: 'Failed',
  suspect_pending: 'Needs review', approve_queued: 'Applying…', rejected: 'Rejected',
  superseded: 'Superseded', running: 'Running', queued: 'Queued', blocked: 'Blocked',
  never: 'Not checked yet', error: 'Error', key_rejected: 'Key rejected',
}

describe('StatusBadge', () => {
  for (const [status, label] of Object.entries(labels)) {
    it(`renders ${status} with label and icon`, () => {
      const w = mount(StatusBadge, { props: { status } })
      expect(w.text()).toContain(label)
      expect(w.find('[data-icon]').exists()).toBe(true)
    })
  }
  it('falls back to the raw status for unknown values', () => {
    const w = mount(StatusBadge, { props: { status: 'weird' } })
    expect(w.text()).toContain('weird')
    expect(w.find('[data-icon]').exists()).toBe(true)
  })
})

describe('StatusBadge watch target error', () => {
  it('shows a failing tone and cross icon for error', () => {
    const w = mount(StatusBadge, { props: { status: 'error' } })
    expect(w.text()).toContain('Error')
    expect(w.get('.badge').classes()).toContain('tone-fail')
    expect(w.get('[data-icon]').attributes('data-icon-name')).toBe('cross')
  })
})
