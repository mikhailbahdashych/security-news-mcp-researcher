import { useEffect, useState } from 'react'

/**
 * Whole seconds since `startedAt`, ticking once a second.
 *
 * The interval belongs to the component that shows the counter, which is
 * mounted only while a turn is on the wire — so it exists exactly as long as
 * there is something to count, and no effect has to remember to stop it. The
 * clock is read at mount and on every tick; `startedAt` is only ever subtracted
 * from it, so a new turn is a new mount rather than a resynchronisation.
 */
export default function useElapsed(startedAt: number): number {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1_000)
    return () => clearInterval(timer)
  }, [])

  return Math.max(0, Math.floor((now - startedAt) / 1_000))
}
