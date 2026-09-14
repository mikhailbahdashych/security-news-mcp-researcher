import { describe, expect, it } from 'vitest'

import { DEFAULT_LAYOUT, pageFromPath, parseLayout, routeForPage } from './layout'
import { parseRail } from './railState'
import { parseStoredTheme, resolveTheme } from './theme'

describe('parseStoredTheme', () => {
  it('accepts the two themes there are', () => {
    expect(parseStoredTheme('light')).toBe('light')
    expect(parseStoredTheme('dark')).toBe('dark')
  })

  it('rejects anything else, including an unset key', () => {
    expect(parseStoredTheme(null)).toBeNull()
    expect(parseStoredTheme('')).toBeNull()
    expect(parseStoredTheme('DARK')).toBeNull()
    expect(parseStoredTheme('system')).toBeNull()
  })
})

describe('resolveTheme', () => {
  it('follows the OS until the user has chosen', () => {
    expect(resolveTheme(null, true)).toBe('dark')
    expect(resolveTheme(null, false)).toBe('light')
  })

  it('lets a stored choice override the OS', () => {
    expect(resolveTheme('light', true)).toBe('light')
    expect(resolveTheme('dark', false)).toBe('dark')
  })

  it('treats junk as no choice at all', () => {
    expect(resolveTheme('purple', true)).toBe('dark')
  })
})

describe('parseRail', () => {
  it('only opens the rail when it was explicitly left open', () => {
    expect(parseRail('expanded')).toBe('expanded')
    expect(parseRail('collapsed')).toBe('collapsed')
    expect(parseRail(null)).toBe('collapsed')
    expect(parseRail('wide')).toBe('collapsed')
  })
})

describe('parseLayout', () => {
  it('defaults to a single pane', () => {
    expect(parseLayout(null)).toEqual(DEFAULT_LAYOUT)
    expect(DEFAULT_LAYOUT.split).toBe(false)
  })

  it('reads a stored layout back', () => {
    expect(parseLayout('{"split":true,"paneA":"notes","paneB":"settings"}')).toEqual({
      split: true,
      paneA: 'notes',
      paneB: 'settings',
    })
  })

  it('survives storage that is not JSON', () => {
    expect(parseLayout('{')).toEqual(DEFAULT_LAYOUT)
    expect(parseLayout('null')).toEqual(DEFAULT_LAYOUT)
    expect(parseLayout('"split"')).toEqual(DEFAULT_LAYOUT)
  })

  it('falls back field by field rather than throwing the whole preference away', () => {
    expect(parseLayout('{"split":true,"paneA":"inbox2","paneB":"notes"}')).toEqual({
      split: true,
      paneA: 'inbox',
      paneB: 'notes',
    })
    // A truthy-but-not-true `split` is not a yes: this is user-editable storage.
    expect(parseLayout('{"split":"yes"}').split).toBe(false)
  })
})

describe('routes', () => {
  it('maps each page to its route and back', () => {
    for (const page of ['inbox', 'research', 'notes', 'settings'] as const) {
      expect(pageFromPath(routeForPage(page))).toBe(page)
    }
  })

  it('keeps detail routes on their page', () => {
    expect(pageFromPath('/chat/12')).toBe('research')
    expect(pageFromPath('/notes/7')).toBe('notes')
    expect(pageFromPath('/settings')).toBe('settings')
  })

  it('treats an unknown path as the inbox, which is what `/` shows', () => {
    expect(pageFromPath('/nope')).toBe('inbox')
    expect(pageFromPath('')).toBe('inbox')
  })
})
