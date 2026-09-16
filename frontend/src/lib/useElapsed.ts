import { useEffect, useState } from 'react'

/**
 * The wall clock, re-read every `tickMs`.
 *
 * The interval belongs to the component that shows the counter, so it lives
 * exactly as long as there is something to count and no effect has to remember
 * to stop it. Mount it in the smallest component that needs it: a clock in a
 * page re-renders the whole page once a second for a line of text.
 *
 * `restartOn` is anything whose change should restart the tick rather than let
 * a new counter inherit the old one's phase.
 */
export function useNow(tickMs: number, restartOn?: unknown): number {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), tickMs)
    return () => clearInterval(timer)
  }, [tickMs, restartOn])

  return now
}

/**
 * Whole seconds since `startedAt`, ticking once a second.
 *
 * `TurnProgress` mounts and unmounts several times in a turn — the line is
 * hidden while text flows — so the clock is re-read often; the value stays
 * right either way, because `startedAt` is only ever subtracted from it.
 *
 * `startedAt` restarts the tick so a new turn does not inherit the old one's
 * phase. It is deliberately not read into state: a `setState` in an effect is a
 * second render for something a subtraction already gives.
 */
export default function useElapsed(startedAt: number): number {
  const now = useNow(1_000, startedAt)
  return Math.max(0, Math.floor((now - startedAt) / 1_000))
}
