/**
 * The bulk "Save to knowledge base" job, as a reducer over its SSE frames.
 *
 * `POST /api/kb/bulk` streams one frame per finished item and ends on a terminal
 * `done` that says what is in the database — including after a Cancel, which is
 * the whole reason the panel can say "eleven were saved before you stopped".
 * Three rules are easy to get wrong and are decided here rather than inline,
 * because getting any of them wrong is silent:
 *
 * - **`total` comes from the frames, never from the ids sent.** The server
 *   de-duplicates `item_ids`, so a selection of 20 with a repeat is a job of 19
 *   and a bar keyed on the request would stop at 95%.
 * - **`done` is terminal, and it is an *event*.** Nothing after it may move the
 *   state, and the stream is never aborted once it has arrived (`generationPhase.ts`
 *   learned that on the notes generator: the body outlives the frame).
 * - **`error` is not the end.** A cancelled run still ends on `done`, so the
 *   error is kept beside the counts rather than replacing them. The one `error`
 *   with no `done` after it is the duplicate-job-id race.
 *
 * The payload rides inside `text_delta` as JSON because the SSE vocabulary is
 * the agent package's and Phase 2 does not widen it.
 */

export type BulkPhase = 'running' | 'done' | 'error'

export interface BulkState {
  phase: BulkPhase
  /** The client-minted id, replaced by the server's own if it sent one. */
  jobId: string
  /** Items finished so far, as the last frame counted them. */
  done: number
  /** The **de-duplicated** size of the job. 0 until the first frame arrives. */
  total: number
  saved: number
  skipped: number
  duplicates: number
  entryIds: number[]
  error: { type: string; message: string } | null
  /** The last reason an item was not saved, so the panel can show one. */
  lastSkip: string | null
}

export const startBulk = (jobId: string): BulkState => ({
  phase: 'running',
  jobId,
  done: 0,
  total: 0,
  saved: 0,
  skipped: 0,
  duplicates: 0,
  entryIds: [],
  error: null,
  lastSkip: null,
})

/** Has the terminal frame arrived? Then the stream must not be aborted. */
export const bulkDelivered = (state: BulkState): boolean => state.phase === 'done'

function parse(data: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(data)
    return value && typeof value === 'object' ? (value as Record<string, unknown>) : null
  } catch {
    return null
  }
}

const int = (value: unknown, fallback = 0): number =>
  typeof value === 'number' && Number.isFinite(value) ? value : fallback

/** One frame in; the next state out. Unknown and malformed frames change nothing. */
export function bulkFrame(state: BulkState, event: string, data: string): BulkState {
  // Past `done` the job is history: a late frame from the tail of the stream
  // must not move a count the panel has already shown as final.
  if (state.phase === 'done') {
    return state
  }
  const payload = parse(data)
  if (payload === null) {
    return state
  }

  switch (event) {
    case 'turn_start': {
      // The server generates a job id when the client sent none; Cancel needs
      // whichever one is real.
      const jobId = payload.job_id
      return typeof jobId === 'string' && jobId ? { ...state, jobId } : state
    }
    case 'text_delta': {
      const text = payload.text
      const item = typeof text === 'string' ? parse(text) : null
      if (item === null) {
        return state
      }
      const reason = typeof item.skipped_reason === 'string' ? item.skipped_reason : null
      return {
        ...state,
        done: int(item.done, state.done),
        total: int(item.total, state.total),
        lastSkip: reason ?? state.lastSkip,
      }
    }
    case 'error': {
      const message = payload.message
      return {
        ...state,
        phase: 'error',
        error: {
          // `type`, not `error_type`: this is `app/agent/events.py`'s shape, the
          // one every stream in the app shares.
          type: typeof payload.type === 'string' ? payload.type : 'api_error',
          message: typeof message === 'string' ? message : 'The bulk save failed.',
        },
      }
    }
    case 'done': {
      const ids = payload.entry_ids
      return {
        ...state,
        phase: 'done',
        saved: int(payload.saved),
        skipped: int(payload.skipped),
        duplicates: int(payload.duplicates),
        entryIds: Array.isArray(ids) ? ids.filter((id): id is number => typeof id === 'number') : [],
      }
    }
    default:
      return state
  }
}

/**
 * What the panel says when it is over.
 *
 * The duplicates are named separately because they only ever arrive here: a
 * bulk run defers every embedding to one call at the end, so the progress
 * frames carry `possible_duplicate_of: null` for even a certain duplicate.
 */
export function bulkSummary(state: BulkState): string {
  const parts = [`${state.saved} saved`]
  if (state.skipped > 0) {
    parts.push(`${state.skipped} skipped`)
  }
  if (state.duplicates > 0) {
    parts.push(
      state.duplicates === 1 ? '1 possible duplicate' : `${state.duplicates} possible duplicates`,
    )
  }
  const counts = parts.join(' · ')
  return state.error?.type === 'cancelled' ? `Stopped. ${counts}.` : `${counts}.`
}
