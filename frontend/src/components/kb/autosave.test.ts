import { describe, expect, it } from 'vitest'

import { autosaveDecision, autosaveLabel, pendingFlush } from './autosave'

const state = {
  pending: 'Patch window: Thursday.',
  saved: '',
  settled: true,
  saving: false,
}

describe('autosaveDecision', () => {
  it('saves once the typing has stopped and the text has moved on', () => {
    expect(autosaveDecision(state)).toBe('save')
  })

  it('waits while the debounce is still behind the textarea', () => {
    // `pending` is the debounced value. Sending it while more keystrokes are
    // already in the box saves a half-written note and then has to save again.
    expect(autosaveDecision({ ...state, settled: false })).toBe('typing')
  })

  it('has nothing to do when the note is what the server already holds', () => {
    expect(autosaveDecision({ ...state, saved: state.pending })).toBe('clean')
  })

  it('never opens a second PATCH over the first', () => {
    // Two in flight can land out of order, and the loser is the newer text.
    expect(autosaveDecision({ ...state, saving: true })).toBe('in-flight')
  })

  it('reads a note typed back to what was saved as clean', () => {
    expect(autosaveDecision({ ...state, pending: 'x', saved: 'x', settled: true })).toBe('clean')
  })
})

describe('autosaveLabel', () => {
  it('says what the editor is doing, and says nothing when it is idle', () => {
    expect(autosaveLabel('in-flight', true)).toBe('Saving…')
    expect(autosaveLabel('typing', true)).toBe('Unsaved changes')
    expect(autosaveLabel('save', true)).toBe('Unsaved changes')
    expect(autosaveLabel('clean', true)).toBe('Saved')
    // Nothing has been saved this visit: "Saved" would be a claim about a note
    // the user has not touched.
    expect(autosaveLabel('clean', false)).toBeNull()
  })

  it('keeps the failure on screen over any of them', () => {
    expect(autosaveLabel('clean', true, true)).toBe('Could not save')
    expect(autosaveLabel('in-flight', true, true)).toBe('Saving…')
  })
})

describe('pendingFlush', () => {
  it('hands back the text the server has not seen', () => {
    // Unmounting cancels the debounce, so the last 1.2 s of typing is only ever
    // sent because of this.
    expect(pendingFlush('Patch window: Thursday.', '')).toBe('Patch window: Thursday.')
  })

  it('has nothing to send when the server already has it', () => {
    expect(pendingFlush('same', 'same')).toBeNull()
    expect(pendingFlush('', '')).toBeNull()
  })

  it('treats an emptied note as an edit, not as nothing to do', () => {
    // Deleting the whole note is a save; `''` is falsy, which is exactly the
    // trap this returns `null` rather than `''` to avoid.
    expect(pendingFlush('', 'Patch window: Thursday.')).toBe('')
  })
})
