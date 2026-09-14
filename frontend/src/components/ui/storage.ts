import { useCallback, useEffect, useState } from 'react'

/**
 * `localStorage` for the shell's three preferences, with the two things a bare
 * `localStorage.getItem` does not give us:
 *
 * - it never throws. Private windows and hardened browsers make storage access
 *   raise, and a preference is not worth a blank screen.
 * - a write notifies every hook reading the same key, so the Settings page
 *   moving a pane and the rail showing which pane is open cannot disagree.
 */

type Listener = () => void

const listeners = new Map<string, Set<Listener>>()

export function readRaw(key: string): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

export function writeRaw(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    // Not persisted, but the in-memory notification below still happens, so the
    // preference holds for this session.
  }
  for (const listener of listeners.get(key) ?? []) {
    listener()
  }
}

export function subscribe(key: string, listener: Listener): () => void {
  const forKey = listeners.get(key) ?? new Set<Listener>()
  forKey.add(listener)
  listeners.set(key, forKey)
  return () => {
    forKey.delete(listener)
  }
}

/**
 * A preference that lives in storage.
 *
 * `parse` and `serialize` must be module-level functions: they are read once per
 * render and never listed as effect dependencies.
 */
export function useStored<T>(
  key: string,
  parse: (raw: string | null) => T,
  serialize: (value: T) => string,
): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => parse(readRaw(key)))

  useEffect(() => {
    const reread = () => setValue(parse(readRaw(key)))
    // A second tab of the same app counts as another writer.
    const onStorage = (event: StorageEvent) => {
      if (event.key === key || event.key === null) {
        reread()
      }
    }
    window.addEventListener('storage', onStorage)
    const unsubscribe = subscribe(key, reread)
    // Storage may have changed between the initial render and this effect.
    reread()
    return () => {
      window.removeEventListener('storage', onStorage)
      unsubscribe()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- parse is module-level.
  }, [key])

  const update = useCallback(
    (next: T) => writeRaw(key, serialize(next)),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- serialize is module-level.
    [key],
  )

  return [value, update]
}
