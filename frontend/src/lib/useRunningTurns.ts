import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'

import { fetchRunningSessions, runningSessionsKey } from '../api/chat'
import { formatElapsed } from '../components/chat/liveTurn'

/**
 * Which sessions have a turn in flight, for the rail's dot and its chat list's
 * `running` marks.
 *
 * **No interval.** A turn is started by a click and ends when the server says
 * so, and this app polls nothing: the triggers are the page's own invalidations
 * (a send, a turn settling) and coming back to the window, which is the moment
 * the answer could have changed while the user was elsewhere. `staleTime` keeps
 * a tab-flick from re-asking.
 */
export function useRunningTurns(): Set<number> {
  const query = useQuery({
    queryKey: runningSessionsKey,
    queryFn: fetchRunningSessions,
    refetchOnWindowFocus: true,
    staleTime: 5_000,
  })
  // Memoised on the answer, not rebuilt per render: the chat page re-renders on
  // every streamed delta, and the list would be handed a new set each time.
  return useMemo(() => new Set(query.data?.session_ids ?? []), [query.data])
}

/**
 * The header's line for a session whose turn is running elsewhere: `running · 1m 05s`.
 *
 * Counts from the server's own start time — the turn is the server's, and the
 * browser may be opening the session minutes after it began. That timestamp is
 * naive UTC, like every other one, so the zone designator has to be put back on
 * or the counter is out by the browser's offset.
 */
export function runningHeaderMeta(startedAt: string | null, now: number): string {
  if (!startedAt) {
    return ''
  }
  const started = Date.parse(`${startedAt}Z`)
  if (Number.isNaN(started)) {
    return ''
  }
  // `formatElapsed` counts in seconds.
  return `running · ${formatElapsed(Math.max(0, now - started) / 1_000)}`
}
