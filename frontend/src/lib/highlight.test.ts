import { describe, expect, it } from 'vitest'

import { splitOnQuery, splitOnTerms } from './highlight'

describe('splitOnQuery', () => {
  it('marks the match and keeps the text around it', () => {
    expect(splitOnQuery('A worm spread through npm', 'worm')).toEqual([
      { text: 'A ', match: false },
      { text: 'worm', match: true },
      { text: ' spread through npm', match: false },
    ])
  })

  it('matches whatever the case, and gives back the original spelling', () => {
    expect(splitOnQuery('ChainDrop worm', 'chaindrop')).toEqual([
      { text: 'ChainDrop', match: true },
      { text: ' worm', match: false },
    ])
  })

  it('marks every occurrence', () => {
    expect(splitOnQuery('npm, npm, npm', 'npm').filter((part) => part.match)).toHaveLength(3)
  })

  it('returns one unmatched run when there is nothing to mark', () => {
    expect(splitOnQuery('A worm', '')).toEqual([{ text: 'A worm', match: false }])
    expect(splitOnQuery('A worm', '   ')).toEqual([{ text: 'A worm', match: false }])
    expect(splitOnQuery('A worm', 'zzz')).toEqual([{ text: 'A worm', match: false }])
  })

  it('adds no empty run when the match is at either end', () => {
    expect(splitOnQuery('worm', 'worm')).toEqual([{ text: 'worm', match: true }])
  })
})

describe('splitOnTerms', () => {
  it('marks each term where it sits, not the phrase', () => {
    // FTS5 matches `"chaindrop" AND "worm"`, which can be paragraphs apart —
    // looking for the literal phrase finds nothing and leaves the hit unmarked.
    const parts = splitOnTerms('A ChainDrop package hid a worm', 'chaindrop worm')
    expect(parts.filter((part) => part.match).map((part) => part.text)).toEqual([
      'ChainDrop',
      'worm',
    ])
    expect(parts.map((part) => part.text).join('')).toBe('A ChainDrop package hid a worm')
  })

  it('is the plain split when the query is one term', () => {
    expect(splitOnTerms('A worm', 'worm')).toEqual(splitOnQuery('A worm', 'worm'))
  })

  it('never marks inside a run another term already claimed', () => {
    // `npm` occurs inside `npmjs`, and a second pass over an already-marked run
    // would nest the marks.
    const parts = splitOnTerms('npmjs registry', 'npmjs npm')
    expect(parts.filter((part) => part.match).map((part) => part.text)).toEqual(['npmjs'])
  })

  it('gives back one unmatched run for an empty query', () => {
    expect(splitOnTerms('A worm', '   ')).toEqual([{ text: 'A worm', match: false }])
  })
})
