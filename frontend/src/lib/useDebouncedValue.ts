import { useEffect, useState } from 'react'

/**
 * The value, but only after it has stopped changing for `delayMs`.
 *
 * Every search box in the app drives a query key, so without this each keystroke
 * would be its own request (and its own cache entry).
 */
export default function useDebouncedValue<T>(value: T, delayMs = 300): T {
  const [settled, setSettled] = useState(value)

  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delayMs)
    return () => clearTimeout(timer)
  }, [value, delayMs])

  return settled
}
