/**
 * When the entry page's notes editor should PATCH, and what it should say.
 *
 * A function rather than a condition inside the effect, because "is this worth
 * saving" weighs three things that change at different speeds — the debounced
 * text, the text in the box, and whether a save is already in flight — and each
 * of the three has already been the reason a note was saved half-written or
 * saved twice.
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
}

export type AutosaveDecision = 'typing' | 'clean' | 'in-flight' | 'save'

export function autosaveDecision(state: AutosaveState): AutosaveDecision {
  if (!state.settled) {
    return 'typing'
  }
  if (state.pending === state.saved) {
    return 'clean'
  }
  // Never two PATCHes over one field: they can land out of order, and the loser
  // is the newer text. The next debounce tick picks the edit up again.
  return state.saving ? 'in-flight' : 'save'
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
  if (failed) {
    return 'Could not save'
  }
  if (decision === 'typing' || decision === 'save') {
    return 'Unsaved changes'
  }
  return savedOnce ? 'Saved' : null
}
