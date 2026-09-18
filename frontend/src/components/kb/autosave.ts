/**
 * When the entry page's notes editor should PATCH, and what it should say.
 *
 * A function rather than a condition inside the effect, because "is this worth
 * saving" weighs four things that change at different speeds — the debounced
 * text, the text in the box, whether a save is already in flight, and whether
 * this exact text has already been refused — and each of the four has already
 * been the reason a note was saved half-written, saved twice, or PATCHed in a
 * loop for as long as the page stayed open.
 */

/** How long the editor waits after the last keystroke before it saves. */
export const AUTOSAVE_QUIET_MS = 1200

export interface AutosaveState {
  /** The debounced text — what would be sent. */
  pending: string
  /** What the server last confirmed. */
  saved: string
  /** The debounce has caught up with the textarea: typing has stopped. */
  settled: boolean
  /** A PATCH is already in flight. */
  saving: boolean
  /** The text the last PATCH was refused for, or `null` — see `'failed'`. */
  failed: string | null
}

export type AutosaveDecision = 'typing' | 'clean' | 'in-flight' | 'failed' | 'save'

export function autosaveDecision(state: AutosaveState): AutosaveDecision {
  if (!state.settled) {
    return 'typing'
  }
  if (state.pending === state.saved) {
    return 'clean'
  }
  // Never two PATCHes over one field: they can land out of order, and the loser
  // is the newer text. The next debounce tick picks the edit up again.
  if (state.saving) {
    return 'in-flight'
  }
  // A mutation does not inherit the app's `retry: 1`, and the effect that acts
  // on this decision re-fires every time the decision flips back. Without this
  // leg a permanent failure — a 422 from an over-long note, or a backend that is
  // not running — is re-sent as fast as `fetch` can reject it. The retry is the
  // *next edit*: the same text that lost will lose again.
  if (state.failed !== null && state.pending === state.failed) {
    return 'failed'
  }
  return 'save'
}

/**
 * The line under the editor.
 *
 * `null` while there is nothing to report — a note nobody has touched must not
 * claim to have been saved this visit.
 */
export function autosaveLabel(
  decision: AutosaveDecision,
  savedOnce: boolean,
  failed = false,
): string | null {
  if (decision === 'in-flight') {
    return 'Saving…'
  }
  if (decision === 'failed') {
    // What happens next is the user's edit, so the line says so: "Could not
    // save" on its own reads as something that is still being retried.
    return 'Could not save — edit the note to try again'
  }
  if (failed) {
    return 'Could not save'
  }
  if (decision === 'typing' || decision === 'save') {
    return 'Unsaved changes'
  }
  return savedOnce ? 'Saved' : null
}

export interface FlushState {
  /** What is in the textarea right now. */
  latest: string
  /** What the server last confirmed. */
  confirmed: string
  /** The text a PATCH is carrying at this moment, or `null`. */
  sending: string | null
}

export interface FlushPlan {
  /** The text to PATCH, or `null` when there is nothing left to send. */
  text: string | null
  /** Wait for the in-flight PATCH to settle before sending this one. */
  afterInFlight: boolean
}

/**
 * What is left to send when the editor goes away, and when it may go out.
 *
 * Unmounting cancels the debounce — `useDebouncedValue` clears its timer in
 * cleanup — so "type a line, click back" inside the quiet window would never
 * reach the server. The editor flushes this on the way out.
 *
 * Two things the naive version got wrong. The text to compare against is what
 * the server will hold *after* the in-flight PATCH, not `confirmed`, which is
 * still the pre-flight value — otherwise leaving mid-save sends a second copy of
 * the text that is already on the wire. And a flush that overtakes that PATCH is
 * the out-of-order write `autosaveDecision` exists to prevent, so it waits.
 *
 * `text: null` means there is nothing to send; `''` means the note was emptied,
 * which is an edit like any other. That is the whole reason this is a nullable
 * string rather than a truthiness check at the call site.
 */
export function flushPlan({ latest, confirmed, sending }: FlushState): FlushPlan {
  const held = sending ?? confirmed
  const text = latest === held ? null : latest
  return { text, afterInFlight: text !== null && sending !== null }
}
