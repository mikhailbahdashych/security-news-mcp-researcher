import { useCallback, useEffect } from 'react'

import { useStored } from './storage'

export type Theme = 'light' | 'dark'

export const THEME_STORAGE_KEY = 'snr.theme'

/** A stored value, if it names a theme we have. */
export function parseStoredTheme(raw: string | null): Theme | null {
  return raw === 'light' || raw === 'dark' ? raw : null
}

/**
 * The theme to show: the user's choice if they made one, otherwise the OS.
 *
 * Pure so it can be tested, and so `index.html`'s pre-paint script can be
 * checked against the same rules it inlines.
 */
export function resolveTheme(raw: string | null, prefersDark: boolean): Theme {
  return parseStoredTheme(raw) ?? (prefersDark ? 'dark' : 'light')
}

function prefersDark(): boolean {
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches
  } catch {
    return false
  }
}

const parse = (raw: string | null): Theme => resolveTheme(raw, prefersDark())
const serialize = (theme: Theme): string => theme

/**
 * The light/dark switch.
 *
 * `index.html` has already put the right `data-theme` on `<html>` before React
 * booted; this hook only keeps it there after a toggle.
 */
export function useTheme(): {
  theme: Theme
  setTheme: (theme: Theme) => void
  toggle: () => void
} {
  const [theme, setTheme] = useStored<Theme>(THEME_STORAGE_KEY, parse, serialize)

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
  }, [theme])

  const toggle = useCallback(
    () => setTheme(theme === 'dark' ? 'light' : 'dark'),
    [theme, setTheme],
  )

  return { theme, setTheme, toggle }
}
