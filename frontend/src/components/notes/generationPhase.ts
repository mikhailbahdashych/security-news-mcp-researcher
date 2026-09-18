/**
 * When the generate dialog hands the note over, and who owns the stream after.
 *
 * `POST /api/notes/generate` sends `done` and only *then* captures the note into
 * the knowledge base — an embedding call that can hang for its whole budget — so
 * the body ends well after the frame the user is waiting for. The dialog
 * therefore acts on the frame and leaves the rest of the stream running without
 * it. Two consequences, decided here rather than inline because getting either
 * wrong is silent:
 *
 * - once the note is handed over the dialog is **unmounted**, so it must stop
 *   touching its own state and must ignore anything that follows;
 * - closing it must no longer abort the request. An abort past `done`
 *   disconnects the client mid-capture, and the server's guard catches
 *   `Exception`, not the `CancelledError` that arrives: the capture would die
 *   with no activity row and nothing in the log.
 */

export type GenerationPhase = 'idle' | 'streaming' | 'delivered'

/**
 * Is this dialog still the owner of the stream?
 *
 * Owning it means two things at once — it may abort the request, and it may
 * still set its own state — and they are the same thing: both are true exactly
 * while the user is the one waiting.
 */
export function ownsStream(phase: GenerationPhase): boolean {
  return phase === 'streaming'
}

/**
 * The note id to hand over for a `done` frame, or `null` to ignore the frame.
 *
 * The id is checked rather than trusted: `done` without one would navigate to
 * `/notes/undefined`, where the error branch — "the generation ended without
 * saving a note" — is what the user needs to see.
 */
export function noteIdOnDone(phase: GenerationPhase, payload: unknown): number | null {
  if (!ownsStream(phase)) {
    return null
  }
  const id = (payload as { note_id?: unknown } | null)?.note_id
  return typeof id === 'number' && Number.isInteger(id) ? id : null
}
