import { useCallback } from 'react'

import { useStored } from './storage'

/** The four pages, by the key the rail, the layout and the routes all use. */
export const PAGE_KEYS = ['inbox', 'research', 'notes', 'settings'] as const

export type PageKey = (typeof PAGE_KEYS)[number]

export const PAGE_LABELS: Record<PageKey, string> = {
  inbox: 'Inbox',
  research: 'Research',
  notes: 'Notes',
  settings: 'Settings',
}

const PAGE_ROUTES: Record<PageKey, string> = {
  inbox: '/',
  research: '/chat',
  notes: '/notes',
  settings: '/settings',
}

export const LAYOUT_STORAGE_KEY = 'snr.layout'

export interface LayoutState {
  /** Two pages side by side. Off by default — one page is the normal app. */
  split: boolean
  /** Right pane. Rendered embedded, with no route of its own. */
  paneB: PageKey
}

/**
 * There is deliberately no `paneA`. The left pane is the router's pane, so the
 * URL is the only statement of what it shows; storing a mirror of it meant two
 * answers to one question, and the stale one won often enough to be a bug. A
 * `paneA` left in storage by an older build parses away with every other
 * unrecognised key.
 */
export const DEFAULT_LAYOUT: LayoutState = { split: false, paneB: 'research' }

function asPageKey(value: unknown, fallback: PageKey): PageKey {
  return (PAGE_KEYS as readonly string[]).includes(value as string)
    ? (value as PageKey)
    : fallback
}

/**
 * The stored layout, or the default.
 *
 * Tolerant on purpose: this is user-editable storage from a previous version of
 * the app, so anything unrecognised falls back field by field rather than
 * throwing the whole preference away.
 */
export function parseLayout(raw: string | null): LayoutState {
  if (raw === null) {
    return DEFAULT_LAYOUT
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return DEFAULT_LAYOUT
  }
  if (parsed === null || typeof parsed !== 'object') {
    return DEFAULT_LAYOUT
  }
  const record = parsed as Record<string, unknown>
  return {
    split: record.split === true,
    paneB: asPageKey(record.paneB, DEFAULT_LAYOUT.paneB),
  }
}

export function serializeLayout(layout: LayoutState): string {
  return JSON.stringify(layout)
}

/** Where the rail sends the browser for a page. */
export function routeForPage(page: PageKey): string {
  return PAGE_ROUTES[page]
}

/** Which page a URL is showing. `/notes/7` is still Notes; `/chat/3` still Research. */
export function pageFromPath(pathname: string): PageKey {
  if (pathname === '/' || pathname === '') {
    return 'inbox'
  }
  if (pathname.startsWith('/chat')) {
    return 'research'
  }
  if (pathname.startsWith('/notes')) {
    return 'notes'
  }
  if (pathname.startsWith('/settings')) {
    return 'settings'
  }
  return 'inbox'
}

export interface LayoutControls extends LayoutState {
  setSplit: (split: boolean) => void
  setPaneB: (page: PageKey) => void
}

/**
 * Split-screen preferences, shared by the shell (which renders them) and the
 * Settings page (which edits them). Both see the same value the moment either
 * one writes.
 */
export function useLayout(): LayoutControls {
  const [layout, setLayout] = useStored<LayoutState>(
    LAYOUT_STORAGE_KEY,
    parseLayout,
    serializeLayout,
  )

  const setSplit = useCallback(
    (split: boolean) => setLayout({ ...layout, split }),
    [layout, setLayout],
  )
  const setPaneB = useCallback(
    (paneB: PageKey) => setLayout({ ...layout, paneB }),
    [layout, setLayout],
  )

  return { ...layout, setSplit, setPaneB }
}
