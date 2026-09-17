import { describe, expect, it } from 'vitest'

import { autosaveDecision, autosaveLabel, flushPlan } from './autosave'

const state = {
  pending: 'Patch window: Thursday.',
  saved: '',
  settled: true,
  saving: false,
  failed: null,
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

  it('does not re-send text the server has already refused', () => {
    // The effect that acts on this decision fires whenever the decision or the
    // text changes, and a mutation does not inherit the app's `retry: 1`. With
    // no gate, a permanent 422 — or a backend that is simply not running — is
    // PATCHed as fast as `fetch` can reject, for as long as the page is open.
    expect(autosaveDecision({ ...state, failed: state.pending })).toBe('failed')
  })

  it('tries again as soon as the text changes', () => {
    expect(autosaveDecision({ ...state, failed: 'an older draft' })).toBe('save')
  })

  it('keeps a save in flight ahead of an older failure', () => {
    expect(autosaveDecision({ ...state, saving: true, failed: state.pending })).toBe('in-flight')
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

  it('says what a stuck save is waiting for', () => {
    // The retry is gated on the text changing, so the line has to say that —
    // "Could not save" alone reads as "keep waiting".
    expect(autosaveLabel('failed', true)).toBe('Could not save — edit the note to try again')
  })

  it('keeps the failure on screen over any of them', () => {
    expect(autosaveLabel('clean', true, true)).toBe('Could not save')
    expect(autosaveLabel('in-flight', true, true)).toBe('Saving…')
  })
})

describe('flushPlan', () => {
  it('hands back the text the server has not seen', () => {
    // Unmounting cancels the debounce, so the last 1.2 s of typing is only ever
    // sent because of this.
    expect(flushPlan({ latest: 'Patch window: Thursday.', confirmed: '', sending: null })).toEqual({
      text: 'Patch window: Thursday.',
      afterInFlight: false,
    })
  })

  it('has nothing to send when the server already has it', () => {
    expect(flushPlan({ latest: 'same', confirmed: 'same', sending: null }).text).toBeNull()
    expect(flushPlan({ latest: '', confirmed: '', sending: null }).text).toBeNull()
  })

  it('treats an emptied note as an edit, not as nothing to do', () => {
    // Deleting the whole note is a save; `''` is falsy, which is exactly the
    // trap this reports as `null` rather than `''` to avoid.
    expect(flushPlan({ latest: '', confirmed: 'Patch window: Thursday.', sending: null }).text).toBe(
      '',
    )
  })

  it('waits for a PATCH that is still in flight', () => {
    // Two writes over one column can land out of order and the loser is the
    // newer text — the very thing `autosaveDecision` refuses to do, so the
    // flush must not do it on the way out either.
    expect(
      flushPlan({ latest: 'and the CAB is Wednesday', confirmed: '', sending: 'Patch window.' }),
    ).toEqual({ text: 'and the CAB is Wednesday', afterInFlight: true })
  })

  it('sends nothing when the in-flight PATCH is already carrying the latest text', () => {
    // `confirmed` is the *pre-flight* value, so comparing against it would send
    // a second copy of the text that is already on the wire.
    expect(
      flushPlan({ latest: 'Patch window.', confirmed: '', sending: 'Patch window.' }),
    ).toEqual({ text: null, afterInFlight: false })
  })
})
