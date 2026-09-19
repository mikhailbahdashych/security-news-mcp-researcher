import { ApiError } from '../../api/client'
import type { KbEmbedPending } from '../../api/kb'

/**
 * The two decisions "Embed now" makes, away from the component that loops.
 *
 * `POST /kb/embed-pending` embeds a bounded slice per call and reports the whole
 * backlog, so the client calls it again while anything is left and the user has
 * not stopped. There is no poller in this application and this is not one — but
 * a loop that cannot decide to end is worse than a poller, and the way it ends
 * is exactly what a component test would never reach.
 */

/** A partial answer, because this reads a payload from a backend that need not
 *  be this bundle's version. */
export type EmbedAnswer = Partial<KbEmbedPending>

/**
 * Should the loop go round again?
 *
 * Four ways out, and the last is the one that is easy to miss:
 *
 * - the user pressed **Stop**;
 * - the backlog is empty (`pending <= 0`) — the ordinary ending;
 * - the call **threw** (409 no key, 502 Voyage), which never reaches here: the
 *   `catch` shows the message and the count on screen stays true, because the
 *   chunks the call did not reach are still pending;
 * - the call embedded **nothing** while chunks are still pending. A backend that
 *   reports a backlog it cannot make progress on would otherwise spin this
 *   forever, burning one request per tick with nothing to show for it.
 */
export function embedAgain(result: EmbedAnswer, stopRequested: boolean): boolean {
  if (stopRequested) {
    return false
  }
  return (result.pending ?? 0) > 0 && (result.embedded ?? 0) > 0
}

/**
 * What the red line under the button says when a call threw.
 *
 * 409 (no Voyage key) and 502 (Voyage refused) both arrive with a sentence the
 * API wrote for exactly this, so it is shown rather than replaced; anything else
 * is the backend not answering at all, which the API cannot have a sentence for.
 */
export const embedProblem = (error: unknown): string =>
  error instanceof ApiError ? error.detail : 'The embedder could not be reached.'
