import { describe, expect, it } from 'vitest'

import type { KbActivity, KbEntry } from '../../api/kb'
import { needsAttention, retryAction } from './attention'

function entry(overrides: Partial<KbEntry> = {}): KbEntry {
  return {
    id: 1,
    kind: 'article',
    title: 'ChainDrop npm worm',
    url: 'https://example.test/chaindrop',
    source_name: null,
    authorship: 'source',
    lang: 'en',
    published_at: null,
    captured_at: '2026-09-16T09:00:00',
    updated_at: '2026-09-16T09:00:00',
    deleted_at: null,
    review_status: 'unreviewed',
    captured_by: 'auto',
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

function activity(overrides: Partial<KbActivity> = {}): KbActivity {
  return {
    id: 100,
    at: '2026-09-16T09:00:00',
    action: 'capture',
    entry_id: 1,
    source: 'star',
    model: null,
    input_tokens: 0,
    output_tokens: 0,
    detail: null,
    ...overrides,
  }
}

describe('needsAttention', () => {
  it('is empty for a knowledge base of ordinary captured articles', () => {
    // Ruling I16, and the reason this strip exists at all: every one of these is
    // unreviewed, because that is the resting state of a capture. A strip that
    // queued them would be a strip nobody reads.
    const entries = [
      entry({ id: 1 }),
      entry({ id: 2, review_status: 'unreviewed', captured_by: 'user' }),
      entry({ id: 3, summary_md: '## Summary', compiled_at: '2026-09-16T10:00:00' }),
    ]
    const trail = [activity({ id: 1, action: 'capture' }), activity({ id: 2, action: 'embed' })]
    expect(needsAttention(entries, trail)).toEqual([])
  })

  it('lists a flagged duplicate, with the entry a merge would fold it into', () => {
    const rows = needsAttention(
      [entry({ id: 9, possible_duplicate_of: 4 }), entry({ id: 4, title: 'The older one' })],
      [],
    )
    expect(rows).toHaveLength(1)
    expect(rows[0].kind).toBe('duplicate')
    expect(rows[0].entryId).toBe(9)
    expect(rows[0].mergeInto).toBe(4)
    expect(rows[0].text).toContain('The older one')
  })

  it('says a duplicate was flagged on the title alone when nothing is embedded', () => {
    const rows = needsAttention([entry({ id: 9, possible_duplicate_of: 4 })], [], {
      embeddingsConfigured: false,
    })
    expect(rows[0].text).toMatch(/title alone/)
  })

  it('lists an unreviewed model-authored entry, and only a model-authored one', () => {
    const rows = needsAttention(
      [
        entry({ id: 5, kind: 'finding', authorship: 'model', review_status: 'unreviewed' }),
        // The same state on a captured article is not a chore.
        entry({ id: 6, authorship: 'source', review_status: 'unreviewed' }),
        // Already read.
        entry({ id: 7, authorship: 'model', review_status: 'reviewed' }),
      ],
      [],
    )
    expect(rows.map((row) => row.entryId)).toEqual([5])
    expect(rows[0].kind).toBe('unreviewed')
  })

  it('lists a capture that failed, and the reason the trail recorded', () => {
    const rows = needsAttention(
      [],
      [activity({ id: 12, action: 'skip', entry_id: null, detail: 'HTTP 403 from the source' })],
    )
    expect(rows).toHaveLength(1)
    expect(rows[0].kind).toBe('failure')
    expect(rows[0].entryId).toBeNull()
    expect(rows[0].text).toContain('HTTP 403 from the source')
  })

  it('tells a failed compile from a successful one by what the trail wrote', () => {
    // A success is the only thing that writes JSON into `detail`; everything
    // else on a compile row is `_skip`'s prose, whatever it happens to say.
    const rows = needsAttention(
      [],
      [
        activity({ id: 20, action: 'compile', detail: '{"applied": true, "tags": []}' }),
        activity({ id: 21, action: 'recompile', entry_id: 2, detail: 'refused (cyber): no' }),
        activity({ id: 22, action: 'budget_hit', entry_id: 3, detail: 'needs about 4000 tokens' }),
      ],
    )
    expect(rows.map((row) => row.entryId)).toEqual([2, 3])
    expect(rows[1].text).toMatch(/budget is spent/)
  })

  it('recognises every compile failure, not the three with a prefix', () => {
    // All six outcomes of `app/kb/compile.py`. The two at the bottom carry the
    // bare `reason` — `_skip` is called with no `detail` — and used to be read
    // as successes, which is the state a brand-new install is in.
    const details = [
      'refused (cyber): the model declined',
      'unusable answer: missing "summary"',
      'api error: HTTP 529 from the API',
      'needs about 4000 tokens',
      'There is no text on this entry to compile.',
      'No Anthropic API key is configured.',
    ]
    const trail = details.map((detail, index) =>
      activity({ id: 50 + index, action: 'compile', entry_id: index + 1, detail }),
    )
    expect(needsAttention([], trail, { maxFailures: 10 })).toHaveLength(details.length)
  })

  it('reads a compile row with no detail at all as a failure', () => {
    // It cannot be a success: a success always carries its JSON.
    const rows = needsAttention([], [activity({ id: 60, action: 'compile', detail: null })])
    expect(rows.map((row) => row.kind)).toEqual(['failure'])
  })

  it('caps the failures — the trail is history, attention is not', () => {
    const trail = Array.from({ length: 9 }, (_, index) =>
      activity({ id: 30 + index, action: 'skip', entry_id: null, detail: `failure ${index}` }),
    )
    expect(needsAttention([], trail)).toHaveLength(5)
    expect(needsAttention([], trail, { maxFailures: 2 })).toHaveLength(2)
  })

  it('lists what is in the bin, so Undo has somewhere to live', () => {
    const rows = needsAttention([], [], {
      deleted: [entry({ id: 8, title: 'Deleted one', deleted_at: '2026-09-17T09:00:00' })],
    })
    expect(rows).toHaveLength(1)
    expect(rows[0].kind).toBe('deleted')
    expect(rows[0].entryId).toBe(8)
  })

  it('draws one row per entry, in a stable order', () => {
    // An unreviewed finding that is also a suspected duplicate is one chore.
    const flaggedFinding = entry({
      id: 5,
      kind: 'finding',
      authorship: 'model',
      review_status: 'unreviewed',
      possible_duplicate_of: 4,
    })
    const rows = needsAttention(
      [flaggedFinding, entry({ id: 4 })],
      [activity({ id: 40, action: 'skip', entry_id: 5, detail: 'already flagged' })],
      { deleted: [entry({ id: 6, deleted_at: '2026-09-17T09:00:00' })] },
    )
    expect(rows.map((row) => row.key)).toEqual(['duplicate-5', 'deleted-6'])
  })

  it('leaves a deleted entry out of every leg but the bin', () => {
    const deleted = entry({ id: 8, deleted_at: '2026-09-17T09:00:00', possible_duplicate_of: 4 })
    const rows = needsAttention([deleted], [], { deleted: [deleted] })
    expect(rows.map((row) => row.kind)).toEqual(['deleted'])
  })

  it('carries the action that would settle each failure, and none on the rest', () => {
    const rows = needsAttention(
      [entry({ id: 1, possible_duplicate_of: 2 }), entry({ id: 2 })],
      [
        activity({ id: 70, action: 'skip', entry_id: 3, detail: 'HTTP 403' }),
        activity({ id: 71, action: 'compile', entry_id: 4, detail: 'refused (cyber)' }),
      ],
      { deleted: [entry({ id: 9, deleted_at: '2026-09-17T09:00:00' })] },
    )
    expect(rows.map((row) => row.retry)).toEqual([null, 'refresh', 'compile', null])
  })
})

describe('retryAction', () => {
  it('re-reads the source only for a capture that did not get the page', () => {
    expect(retryAction(activity({ action: 'skip' }))).toBe('refresh')
  })

  it('compiles for every compile failure, including a spent budget', () => {
    // Refreshing these fetches the article again and leaves the entry exactly
    // as uncompiled as it was — and on a note or a finding there is no URL to
    // fetch at all.
    expect(retryAction(activity({ action: 'compile' }))).toBe('compile')
    expect(retryAction(activity({ action: 'recompile' }))).toBe('compile')
    expect(retryAction(activity({ action: 'budget_hit' }))).toBe('compile')
  })
})
