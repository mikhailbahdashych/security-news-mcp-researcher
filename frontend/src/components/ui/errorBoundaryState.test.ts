import { describe, expect, it } from 'vitest'

import { nextBoundaryState } from './errorBoundaryState'

describe('nextBoundaryState', () => {
  it('clears a caught error when the reset key changes', () => {
    expect(nextBoundaryState({ failed: true, resetKey: '/chat/12' }, '/chat/13')).toEqual({
      failed: false,
      resetKey: '/chat/13',
    })
  })

  it('stays caught while the key is the same', () => {
    expect(nextBoundaryState({ failed: true, resetKey: '/settings' }, '/settings')).toBeNull()
  })

  it('tracks the key of a boundary that has not caught', () => {
    expect(nextBoundaryState({ failed: false, resetKey: '/knowledge/7' }, '/knowledge')).toEqual({
      failed: false,
      resetKey: '/knowledge',
    })
  })

  it('never resets a boundary that was given no key', () => {
    expect(nextBoundaryState({ failed: true, resetKey: undefined }, undefined)).toBeNull()
  })
})
