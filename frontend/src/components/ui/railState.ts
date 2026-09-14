import { useCallback } from 'react'

import { useStored } from './storage'

export type RailState = 'collapsed' | 'expanded'

export const RAIL_STORAGE_KEY = 'snr.rail'

/** Collapsed unless the user has asked for the wide rail. */
export function parseRail(raw: string | null): RailState {
  return raw === 'expanded' ? 'expanded' : 'collapsed'
}

const serialize = (state: RailState): string => state

/** Width of the rail, in px, for each state — the shell and the rail agree here. */
export const RAIL_WIDTH: Record<RailState, number> = { collapsed: 58, expanded: 198 }

export function useRail(): {
  state: RailState
  expanded: boolean
  setExpanded: (expanded: boolean) => void
  toggle: () => void
} {
  const [state, setState] = useStored<RailState>(RAIL_STORAGE_KEY, parseRail, serialize)
  const expanded = state === 'expanded'

  const setExpanded = useCallback(
    (next: boolean) => setState(next ? 'expanded' : 'collapsed'),
    [setState],
  )
  const toggle = useCallback(() => setExpanded(!expanded), [expanded, setExpanded])

  return { state, expanded, setExpanded, toggle }
}
