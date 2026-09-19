import { describe, expect, it } from 'vitest'

import { bulkDelivered, bulkFrame, bulkSummary, startBulk, type BulkState } from './bulkSave'

const progress = (fields: Record<string, unknown>) => JSON.stringify({ text: JSON.stringify(fields) })

function run(frames: [string, string][], state: BulkState = startBulk('job-1')): BulkState {
  return frames.reduce((current, [event, data]) => bulkFrame(current, event, data), state)
}

describe('bulkFrame', () => {
  it('starts with nothing counted — the size of the job is the server’s to say', () => {
    const state = startBulk('job-1')
    expect(state.phase).toBe('running')
    expect(state.total).toBe(0)
    expect(bulkDelivered(state)).toBe(false)
  })

  it('takes the job id the server generated, when the client sent none', () => {
    const state = bulkFrame(startBulk('job-1'), 'turn_start', '{"turn": 0, "job_id": "5f3c"}')
    expect(state.jobId).toBe('5f3c')
  })

  it('keys the progress on `total`, which the server de-duplicated', () => {
    // 20 ids were sent with one repeat; the job is 19 items and a bar keyed on
    // the request would stop at 95%.
    const state = run([
      ['turn_start', '{"turn": 0, "job_id": "job-1"}'],
      ['text_delta', progress({ item_id: 12, entry_id: 3, created: true, done: 1, total: 19 })],
      ['text_delta', progress({ item_id: 13, entry_id: 4, created: true, done: 2, total: 19 })],
    ])
    expect(state.done).toBe(2)
    expect(state.total).toBe(19)
  })

  it('keeps the last reason an item was not saved', () => {
    const state = run([
      ['text_delta', progress({ item_id: 12, entry_id: null, skipped_reason: 'too short', done: 1, total: 2 })],
    ])
    expect(state.lastSkip).toBe('too short')
  })

  it('reads the counts off the terminal frame and stops there', () => {
    const state = run([
      ['text_delta', progress({ item_id: 12, done: 1, total: 2 })],
      ['done', '{"saved": 2, "skipped": 0, "duplicates": 1, "entry_ids": [3, 4]}'],
      // Anything after the terminal frame is the tail of a stream that is over.
      ['text_delta', progress({ item_id: 99, done: 99, total: 99 })],
    ])
    expect(bulkDelivered(state)).toBe(true)
    expect(state.saved).toBe(2)
    expect(state.duplicates).toBe(1)
    expect(state.entryIds).toEqual([3, 4])
    expect(state.done).toBe(1)
  })

  it('keeps the counts after a cancel, because `done` still arrives', () => {
    const state = run([
      ['text_delta', progress({ item_id: 12, done: 1, total: 8 })],
      ['error', '{"type": "cancelled", "message": "The turn was stopped before it finished."}'],
      ['done', '{"saved": 1, "skipped": 0, "duplicates": 0, "entry_ids": [3]}'],
    ])
    expect(state.phase).toBe('done')
    expect(state.error?.type).toBe('cancelled')
    expect(state.saved).toBe(1)
    expect(bulkSummary(state)).toBe('Stopped. 1 saved.')
  })

  it('ends on the error alone when a duplicate job id lost the race', () => {
    const state = run([['error', '{"type": "api_error", "message": "A turn is already running."}']])
    expect(state.phase).toBe('error')
    expect(state.error?.message).toBe('A turn is already running.')
    expect(bulkDelivered(state)).toBe(false)
  })

  it('reads the error key the app’s streams actually use', () => {
    // `type`, not `error_type`: `app/agent/events.py` is the one shape.
    const state = run([['error', '{"type": "cancelled", "message": "Stopped."}']])
    expect(state.error?.type).toBe('cancelled')
  })

  it('ignores a second `done`, whatever it claims', () => {
    // Insurance for a shape dependency: the panel is only correct because
    // `run_bulk_capture` never yields `ev.Done` and the route's own terminal
    // frame is the single `done` on the wire. A second one saying nothing was
    // saved would otherwise freeze the panel at `0 saved`.
    const state = run([
      ['done', '{"saved": 12, "skipped": 0, "duplicates": 1, "entry_ids": [3, 4]}'],
      ['done', '{"saved": 0, "skipped": 0, "duplicates": 0, "entry_ids": []}'],
    ])
    expect(state.saved).toBe(12)
    expect(state.entryIds).toEqual([3, 4])
  })

  it('ignores a frame it cannot read rather than losing the run', () => {
    const start = startBulk('job-1')
    expect(run([['text_delta', 'not json']], start)).toBe(start)
    expect(run([['text_delta', '{"text": "not json either"}']], start)).toBe(start)
    expect(run([['ping', '{}']], start)).toBe(start)
  })
})

describe('bulkSummary', () => {
  it('names the duplicates separately — they only arrive on `done`', () => {
    const state = run([
      ['done', '{"saved": 12, "skipped": 3, "duplicates": 2, "entry_ids": []}'],
    ])
    expect(bulkSummary(state)).toBe('12 saved · 3 skipped · 2 possible duplicates.')
  })

  it('says only what happened', () => {
    const state = run([['done', '{"saved": 1, "skipped": 0, "duplicates": 0, "entry_ids": [1]}']])
    expect(bulkSummary(state)).toBe('1 saved.')
  })
})
