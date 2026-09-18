import { describe, expect, it } from 'vitest'

import { noteIdOnDone, ownsStream } from './generationPhase'

describe('noteIdOnDone', () => {
  it('hands the note over on the frame, not at the end of the body', () => {
    expect(noteIdOnDone('streaming', { note_id: 12 })).toBe(12)
  })

  it('ignores a second done: the note has already been handed over', () => {
    // The dialog is unmounted by then. Acting again would navigate twice and
    // update the state of a component that is gone.
    expect(noteIdOnDone('delivered', { note_id: 12 })).toBeNull()
  })

  it('ignores a done that arrives with no stream of ours running', () => {
    expect(noteIdOnDone('idle', { note_id: 12 })).toBeNull()
  })

  it('is not a handover without a usable note id', () => {
    // `/notes/undefined` is the failure this prevents: the route only sends
    // `done` with an id, so anything else is a stream that did not save a note
    // and belongs in the error branch.
    expect(noteIdOnDone('streaming', {})).toBeNull()
    expect(noteIdOnDone('streaming', null)).toBeNull()
    expect(noteIdOnDone('streaming', 12)).toBeNull()
    expect(noteIdOnDone('streaming', { note_id: '12' })).toBeNull()
    expect(noteIdOnDone('streaming', { note_id: 1.5 })).toBeNull()
  })
})

describe('ownsStream', () => {
  it('owns the stream only while one is running', () => {
    expect(ownsStream('streaming')).toBe(true)
    expect(ownsStream('idle')).toBe(false)
  })

  it('lets go at done, so closing the dialog cannot abort the capture', () => {
    // The route captures into the knowledge base *after* `done`. An abort there
    // disconnects the client, which cancels the capture with a `CancelledError`
    // the server's guard does not catch: no activity row, no log line.
    expect(ownsStream('delivered')).toBe(false)
  })
})
