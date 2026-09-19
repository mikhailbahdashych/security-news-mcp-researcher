import { describe, expect, it } from 'vitest'

import {
  budgetLabel,
  compileMetaLabel,
  compileOutcome,
  cveChips,
  duplicateLabel,
  embeddingStatus,
  entityChips,
  entityFilter,
  entityHint,
  entryTimestamp,
  formatTokens,
  groupEntriesByDay,
  hitSnippet,
  kbEntryLink,
  kindLabel,
  matchMarker,
  parseEntryId,
  refreshFailed,
  refreshMessage,
  searchModeLabel,
  sinceDaysAgo,
  sourceLabel,
  summaryStale,
  vecVersionLabel,
  type KbBudget,
  type KbEntry,
  type KbRefreshResult,
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
    compile_model: null,
    compile_prompt_version: null,
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

describe('entityChips', () => {
  it('labels every entity, in order', () => {
    const chips = entityChips({
      entities: [
        { kind: 'cve', value: 'CVE-2026-1111', source: 'regex' },
        { kind: 'vendor', value: 'Acme', source: 'model' },
      ],
    })
    expect(chips).toEqual(['cve: CVE-2026-1111', 'vendor: Acme'])
  })

  it('draws one chip however many sources claimed it', () => {
    // `entities` is not deduplicated across `source` — the regex pass and the
    // compile both find the same CVE — and the page used to key its chips on
    // the label, so the duplicate was a duplicate React key as well as a
    // duplicate chip.
    const chips = entityChips({
      entities: [
        { kind: 'cve', value: 'CVE-2026-1111', source: 'regex' },
        { kind: 'cve', value: 'CVE-2026-1111', source: 'model' },
        { kind: 'cve', value: 'CVE-2026-1111', source: 'user' },
      ],
    })
    expect(chips).toEqual(['cve: CVE-2026-1111'])
  })

  it('keeps two entities that only share a kind', () => {
    const chips = entityChips({
      entities: [
        { kind: 'vendor', value: 'Acme', source: 'model' },
        { kind: 'vendor', value: 'Globex', source: 'model' },
      ],
    })
    expect(chips).toEqual(['vendor: Acme', 'vendor: Globex'])
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

describe('entityHint', () => {
  it('says nothing about a box that is empty or already a filter', () => {
    expect(entityHint('')).toBeNull()
    expect(entityHint('   ')).toBeNull()
    expect(entityHint('cve:CVE-2026-60004')).toBeNull()
    expect(entityHint('CVE-2026-60004')).toBeNull()
  })

  it('names the form when the text is not one', () => {
    // Declining to send is right, but silent: the box keeps the text, the list
    // shows everything, and nothing says the two have nothing to do with each
    // other.
    expect(entityHint('openssl')).toBe('Needs kind:value — for example cve:CVE-2026-1234')
    expect(entityHint('cve:')).toBe('Needs kind:value — for example cve:CVE-2026-1234')
    expect(entityHint(':CVE-2026-60004')).toBe('Needs kind:value — for example cve:CVE-2026-1234')
  })
})

describe('vecVersionLabel', () => {
  it('names the loaded version', () => {
    expect(vecVersionLabel('v0.1.7-alpha.2')).toBe('v0.1.7-alpha.2')
  })

  it('reads the empty string the backend sends for a missing extension as absent', () => {
    // `extension_status` catches the missing `vec_version()` and reports `""`,
    // typed `str` — so `?? 'not loaded'` never fired and the row drew a label
    // with nothing beside it.
    expect(vecVersionLabel('')).toBe('not loaded')
    expect(vecVersionLabel('   ')).toBe('not loaded')
    expect(vecVersionLabel(null)).toBe('not loaded')
    // An older backend that has no such field at all.
    expect(vecVersionLabel(undefined)).toBe('not loaded')
  })
})

function refreshed(overrides: Partial<KbRefreshResult> = {}): KbRefreshResult {
  return { entry: entry(), changed: false, version: 1, status: 'unchanged', reason: null, ...overrides }
}

describe('refreshMessage', () => {
  it('names the new version when the text moved', () => {
    expect(refreshMessage(refreshed({ changed: true, version: 2, status: 'updated' }))).toBe(
      'Re-read — now v2',
    )
  })

  it('says the source has not moved only when that is what happened', () => {
    expect(refreshMessage(refreshed())).toBe('Re-read — unchanged')
  })

  it('reports a failed re-read as a failure, with the reason', () => {
    // A failed fetch is a 200 with `changed: false`, exactly like an unchanged
    // page — so a client reading only that flag told the user a Cloudflare block
    // was "the source has not moved".
    expect(
      refreshMessage(refreshed({ status: 'failed', reason: 'HTTP 403 from the source' })),
    ).toBe('Refresh failed: HTTP 403 from the source')
  })

  it('still says it failed when the backend sent no reason', () => {
    expect(refreshMessage(refreshed({ status: 'failed', reason: null }))).toBe(
      'Refresh failed: the source could not be re-read',
    )
  })

  it('marks the failures, and only those, for the red tone', () => {
    expect(refreshFailed(refreshed())).toBe(false)
    expect(refreshFailed(refreshed({ changed: true, version: 2, status: 'updated' }))).toBe(false)
    expect(refreshFailed(refreshed({ status: 'failed', reason: 'HTTP 403' }))).toBe(true)
  })

  it('reads a reason from a backend that sends no status as a failure', () => {
    const older = { entry: entry(), changed: false, version: 1, reason: 'timed out' }
    expect(refreshMessage(older as KbRefreshResult)).toBe('Refresh failed: timed out')
  })
})

// ------------------------------------------------- Phase 2: tokens and budget

describe('formatTokens', () => {
  it('leaves small counts alone and shortens the large ones', () => {
    expect(formatTokens(0)).toBe('0')
    expect(formatTokens(999)).toBe('999')
    expect(formatTokens(1_000)).toBe('1.0K')
    expect(formatTokens(12_340)).toBe('12.3K')
    // The monthly ceiling is the one number here that reaches seven figures,
    // and `5000k` is not a number anybody reads as five million.
    expect(formatTokens(5_000_000)).toBe('5.0M')
  })

  it('does not render a negative as a token count', () => {
    expect(formatTokens(-12)).toBe('0')
    expect(formatTokens(Number.NaN)).toBe('0')
  })
})

function budget(overrides: Partial<KbBudget> = {}): KbBudget {
  return {
    month: '2026-09',
    limit: 5_000_000,
    anthropic_input: 120_345,
    anthropic_output: 8_801,
    anthropic_total: 129_146,
    remaining: 4_870_854,
    exhausted: false,
    voyage: 412_000,
    voyage_estimated: true,
    ...overrides,
  }
}

describe('budgetLabel', () => {
  it('reads the month against the limit', () => {
    const label = budgetLabel(budget())
    expect(label.used).toBe('129.1K')
    expect(label.limit).toBe('5.0M')
    expect(label.pct).toBe(3)
    expect(label.exhausted).toBe(false)
  })

  it('flips to spent exactly at the limit, and stays there past it', () => {
    expect(budgetLabel(budget({ anthropic_total: 4_999_999 })).exhausted).toBe(false)
    expect(budgetLabel(budget({ anthropic_total: 5_000_000 })).exhausted).toBe(true)
    const over = budgetLabel(budget({ anthropic_total: 6_000_000 }))
    expect(over.exhausted).toBe(true)
    // The bar cannot run past its own end.
    expect(over.pct).toBe(100)
  })

  it('reads a limit of zero as "no compiling at all" rather than dividing by it', () => {
    const none = budgetLabel(budget({ limit: 0, anthropic_total: 0 }))
    expect(none.pct).toBe(100)
    expect(none.exhausted).toBe(true)
  })
})

describe('embeddingStatus', () => {
  it('says nothing when every chunk is embedded', () => {
    expect(embeddingStatus({ chunks: 11, pending_chunks: 0 })).toBeNull()
    expect(embeddingStatus({ chunks: 0, pending_chunks: 0 })).toBeNull()
  })

  it('counts the embedded ones, because no API field carries them', () => {
    expect(embeddingStatus({ chunks: 11, pending_chunks: 3 })).toBe('8 of 11 chunks embedded')
  })
})

describe('searchModeLabel', () => {
  it('says hybrid when the server says hybrid', () => {
    expect(searchModeLabel('hybrid', true)).toMatch(/meaning/i)
  })

  it('points at the missing key when the search was keywords only', () => {
    expect(searchModeLabel('keyword', false)).toMatch(/Voyage/)
  })

  it('points at Embed now when a key is configured but nothing is embedded', () => {
    expect(searchModeLabel('keyword', true)).toMatch(/Embed now/)
  })

  it('has nothing to say when nothing was searched', () => {
    expect(searchModeLabel(undefined, true)).toBeNull()
  })
})

describe('duplicateLabel', () => {
  it('names the entry it would be folded into', () => {
    const newer = entry({ id: 9, possible_duplicate_of: 4 })
    const older = entry({ id: 4, title: 'ChainDrop worm hits npm' })
    expect(duplicateLabel(newer, older)).toBe('Possible duplicate of “ChainDrop worm hits npm”')
  })

  it('falls back to the id when the other entry is not on this page', () => {
    expect(duplicateLabel(entry({ id: 9, possible_duplicate_of: 4 }), undefined)).toBe(
      'Possible duplicate of entry #4',
    )
  })

  it('admits that without a key the flag is the title alone', () => {
    // Two unrelated advisories with similar headlines flag each other, and the
    // row has to say why before anyone merges them.
    expect(duplicateLabel(entry({ possible_duplicate_of: 4 }), undefined, false)).toMatch(
      /title alone/,
    )
  })
})

describe('compileOutcome', () => {
  it('is a sentence for every reason code, not an error', () => {
    expect(compileOutcome({ compiled: true, reason: null, reason_code: null })).toBe('Summarised.')
    expect(compileOutcome({ compiled: false, reason: null, reason_code: 'budget' })).toMatch(
      /budget is spent/,
    )
    expect(compileOutcome({ compiled: false, reason: null, reason_code: 'refusal' })).toMatch(
      /declined/,
    )
    expect(compileOutcome({ compiled: false, reason: null, reason_code: 'parse' })).toMatch(
      /not in a shape/,
    )
    expect(compileOutcome({ compiled: false, reason: null, reason_code: 'no_text' })).toMatch(
      /no captured text/,
    )
    expect(compileOutcome({ compiled: false, reason: null, reason_code: 'api_error' })).toMatch(
      /did not get through/,
    )
  })

  it('keeps the backend’s own detail after the sentence', () => {
    expect(
      compileOutcome({ compiled: false, reason: 'cyber: no', reason_code: 'refusal' }),
    ).toMatch(/cyber: no$/)
  })

  it('still says something when a newer backend sends a code this build has no word for', () => {
    expect(
      compileOutcome({
        compiled: false,
        reason: null,
        reason_code: 'quota' as unknown as null,
      }),
    ).toBe('Not summarised.')
  })
})

describe('compileMetaLabel and summaryStale', () => {
  it('names the model that answered and the prompt it answered', () => {
    expect(
      compileMetaLabel(entry({ compile_model: 'claude-sonnet-5', compile_prompt_version: 3 })),
    ).toBe('claude-sonnet-5 · prompt v3')
    expect(compileMetaLabel(entry())).toBeNull()
  })

  it('calls a summary stale only once the entry moved after it', () => {
    const compiled = entry({
      summary_md: '## Summary',
      compiled_at: '2026-09-16T09:00:00',
      updated_at: '2026-09-16T09:00:00',
    })
    expect(summaryStale(compiled)).toBe(false)
    expect(summaryStale({ ...compiled, updated_at: '2026-09-17T10:00:00' })).toBe(true)
    // Nothing to be stale about.
    expect(summaryStale(entry())).toBe(false)
  })
})
