import { describe, expect, it } from 'vitest'

import {
  cveChips,
  entityFilter,
  entryTimestamp,
  groupEntriesByDay,
  hitSnippet,
  kbEntryLink,
  kindLabel,
  matchMarker,
  parseEntryId,
  sinceDaysAgo,
  sourceLabel,
  type KbEntry,
  type KbHit,
} from './kb'

function entry(overrides: Partial<KbEntry> = {}): KbEntry {
  return {
    id: 1,
    kind: 'article',
    title: 'ChainDrop npm worm',
    url: 'https://www.bleepingcomputer.com/news/chaindrop/',
    source_name: null,
    authorship: 'source',
    lang: 'en',
    published_at: null,
    captured_at: '2026-09-16T09:00:00',
    updated_at: '2026-09-16T09:00:00',
    deleted_at: null,
    review_status: 'unreviewed',
    captured_by: 'user',
    snapshot_chars: 4200,
    snapshot_version: 1,
    summary_md: null,
    notes_md: '',
    compiled_at: null,
    entities: [],
    topics: [],
    tags: [],
    links: { feed_item_id: null, note_id: null, session_id: null },
    possible_duplicate_of: null,
    chunks: 3,
    pending_chunks: 0,
    ...overrides,
  }
}

function hit(overrides: Partial<KbHit> = {}): KbHit {
  return {
    entry: entry(),
    snippet: 'A self-propagating worm spread through npm on 4 August 2026.',
    score: 0.5,
    matched_by: 'keyword',
    ...overrides,
  }
}

describe('kbEntryLink', () => {
  it('is the route the rail and the timeline both open', () => {
    expect(kbEntryLink(7)).toBe('/knowledge/7')
  })
})

describe('parseEntryId', () => {
  it('accepts a positive integer id', () => {
    expect(parseEntryId('7')).toBe(7)
  })

  it('refuses everything the API would answer 422 to', () => {
    // `Number('abc')` is NaN and `Number('1.5')` is 1.5; both reach the API as a
    // 422, which is a status the 404 path cannot act on.
    expect(parseEntryId('abc')).toBeNull()
    expect(parseEntryId('1.5')).toBeNull()
    expect(parseEntryId('0')).toBeNull()
    expect(parseEntryId('-2')).toBeNull()
    expect(parseEntryId(undefined)).toBeNull()
  })
})

describe('entryTimestamp', () => {
  it('is when it was published, or when it was captured', () => {
    // The backend orders on `COALESCE(published_at, captured_at)`; the timeline
    // has to date every row by the same expression or a row lands under a header
    // it did not sort into.
    expect(entryTimestamp(entry({ published_at: '2026-09-14T08:00:00' }))).toBe(
      '2026-09-14T08:00:00',
    )
    expect(entryTimestamp(entry({ published_at: null }))).toBe('2026-09-16T09:00:00')
  })
})

describe('groupEntriesByDay', () => {
  it('groups on the same date the rows are ordered by', () => {
    const days = groupEntriesByDay([
      entry({ id: 3, published_at: '2026-09-16T18:00:00' }),
      // Captured today, published nowhere: it dates by its capture.
      entry({ id: 2, published_at: null, captured_at: '2026-09-16T09:00:00' }),
      entry({ id: 1, published_at: '2026-09-15T22:00:00' }),
    ])
    expect(days.map((day) => day.label)).toEqual(['16 Sep 2026', '15 Sep 2026'])
    expect(days[0].rows.map((row) => row.id)).toEqual([3, 2])
  })
})

describe('matchMarker', () => {
  it('names each leg the way the spec words it', () => {
    expect(matchMarker('keyword').label).toBe('keyword')
    expect(matchMarker('vector').label).toBe('vector')
    expect(matchMarker('both').label).toBe('both')
    // The entity leg is an exact identifier match — "entity" is the backend's
    // word for it and "exact" is the user's.
    expect(matchMarker('entity').label).toBe('exact')
  })

  it('explains itself in a tooltip and marks the exact leg out', () => {
    expect(matchMarker('entity').tone).toBe('accent')
    expect(matchMarker('keyword').tone).toBe('neutral')
    expect(matchMarker('vector').title).toMatch(/meaning/i)
  })

  it('shows an unknown leg rather than hiding the hit', () => {
    // A value from a newer backend is still a hit; it just has no label of ours.
    expect(matchMarker('semantic' as 'keyword').label).toBe('semantic')
    expect(matchMarker('semantic' as 'keyword').tone).toBe('neutral')
  })
})

describe('hitSnippet', () => {
  it('is the passage the search matched', () => {
    expect(hitSnippet(hit())).toBe('A self-propagating worm spread through npm on 4 August 2026.')
  })

  it('says nothing when the snippet is the title again', () => {
    // An entity hit with no body chunk falls back to the entry's title, and the
    // same sentence twice in a row reads as a rendering bug.
    expect(hitSnippet(hit({ snippet: 'ChainDrop npm worm' }))).toBe('')
    expect(hitSnippet(hit({ snippet: '  ChainDrop npm worm  ' }))).toBe('')
  })
})

describe('cveChips', () => {
  it('keeps the CVEs, in order, and nothing else', () => {
    const chips = cveChips(
      entry({
        entities: [
          { kind: 'vendor', value: 'Acme', source: 'model' },
          { kind: 'cve', value: 'CVE-2026-1111', source: 'regex' },
          { kind: 'cve', value: 'CVE-2026-2222', source: 'regex' },
        ],
      }),
    )
    expect(chips).toEqual(['CVE-2026-1111', 'CVE-2026-2222'])
  })

  it('shows one chip per CVE however many rows claimed it', () => {
    // The same id can arrive from the regex pass and from the model.
    const chips = cveChips(
      entry({
        entities: [
          { kind: 'cve', value: 'CVE-2026-1111', source: 'regex' },
          { kind: 'cve', value: 'CVE-2026-1111', source: 'model' },
        ],
      }),
    )
    expect(chips).toEqual(['CVE-2026-1111'])
  })

  it('stops at the cap and says how many are left', () => {
    const entities = ['1111', '2222', '3333', '4444', '5555'].map((tail) => ({
      kind: 'cve',
      value: `CVE-2026-${tail}`,
      source: 'regex',
    }))
    expect(cveChips(entry({ entities }), 3)).toEqual([
      'CVE-2026-1111',
      'CVE-2026-2222',
      'CVE-2026-3333',
      '+2 more',
    ])
  })
})

describe('sourceLabel', () => {
  it('prefers the name the capture recorded', () => {
    expect(sourceLabel(entry({ source_name: 'BleepingComputer' }))).toBe('BleepingComputer')
  })

  it('falls back to the host, without the www', () => {
    expect(sourceLabel(entry({ source_name: null }))).toBe('bleepingcomputer.com')
  })

  it('has nothing to say about an entry with neither', () => {
    expect(sourceLabel(entry({ source_name: null, url: null }))).toBe('')
  })
})

describe('kindLabel', () => {
  it('names the four kinds the API can answer with', () => {
    expect(kindLabel('article')).toBe('article')
    expect(kindLabel('note')).toBe('note')
    expect(kindLabel('manual')).toBe('saved')
    // Phase 2 captures these; the page has to be able to draw one before then.
    expect(kindLabel('finding')).toBe('AI finding')
  })
})

describe('sinceDaysAgo', () => {
  it('is N days back, as the naive-UTC string the API parses', () => {
    expect(sinceDaysAgo(7, new Date('2026-09-17T10:00:00Z'))).toBe('2026-09-10T10:00:00')
  })

  it('carries no zone designator and no fraction', () => {
    // The backend parses naive UTC; a `Z` or a `.123` is a different string to
    // SQLite's comparison, which is plain text.
    expect(sinceDaysAgo(30, new Date('2026-01-05T00:00:00.456Z'))).toBe('2025-12-06T00:00:00')
  })
})

describe('entityFilter', () => {
  it('passes a qualified value straight through, lower-casing the kind', () => {
    expect(entityFilter('cve:CVE-2026-60004')).toBe('cve:CVE-2026-60004')
    expect(entityFilter('Vendor:Acme')).toBe('vendor:Acme')
  })

  it('qualifies a bare CVE id, because that is what people paste', () => {
    expect(entityFilter('CVE-2026-60004')).toBe('cve:CVE-2026-60004')
    expect(entityFilter('  cve-2026-60004  ')).toBe('cve:CVE-2026-60004')
  })

  it('is nothing when there is nothing to filter by', () => {
    // The API reads an unqualified value as no filter at all (`parse_entity`
    // wants a `kind:value`), so sending one would silently widen the search.
    expect(entityFilter('')).toBeNull()
    expect(entityFilter('   ')).toBeNull()
    expect(entityFilter('acme')).toBeNull()
    expect(entityFilter('cve:')).toBeNull()
    expect(entityFilter(':CVE-2026-60004')).toBeNull()
  })
})
