import { describe, expect, it } from 'vitest'

import { splitOnQuery } from './highlight'

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
