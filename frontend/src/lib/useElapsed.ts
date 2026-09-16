import { useEffect, useState } from 'react'

/**
 * Whole seconds since `startedAt`, ticking once a second.
 *
 * The interval belongs to the component that shows the counter, so it lives
 * exactly as long as there is something to count and no effect has to remember
 * to stop it. `TurnProgress` mounts and unmounts several times in a turn — the
 * line is hidden while text flows — so the clock is re-read often; the value
 * stays right either way, because `startedAt` is only ever subtracted from it.
 *
 * `startedAt` is a dependency so a new turn restarts the tick instead of
 * inheriting the old one's phase. It is deliberately not read into state: a
 * `setState` in an effect is a second render for something a subtraction
 * already gives.
 */
export default function useElapsed(startedAt: number): number {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1_000)
    return () => clearInterval(timer)
  }, [startedAt])

  return Math.max(0, Math.floor((now - startedAt) / 1_000))
}
