import { describe, expect, it } from 'vitest'

import {
  citationFor,
  collectSources,
  formatMs,
  formatTokens,
  groupTurns,
  stepsFromMessage,
  toolCallStatus,
  toolHint,
  toolTag,
  whenLabel,
  type ChatMessage,
  type ToolCallRow,
} from './chat'

function row(overrides: Partial<ToolCallRow> = {}): ToolCallRow {
  return {
    id: 1,
    tool_use_id: 'toolu_1',
    name: 'search_feed_items',
    server_name: null,
    source: 'builtin',
    input_json: { q: 'CVE-2026-1234' },
    result_json: null,
    is_error: false,
    duration_ms: null,
    created_at: '2026-09-14T12:00:00',
    ...overrides,
  }
}

describe('toolCallStatus', () => {
  it('reports a call with no result yet as running', () => {
    // What a mid-turn reload reads: the row is written when the call starts and
    // filled in when it comes back. `is_error` alone rendered this as a tick.
    expect(toolCallStatus(row({ result_json: null }))).toBe('running')
  })

  it('reports a finished call as ok', () => {
    expect(toolCallStatus(row({ result_json: { content: 'three items' } }))).toBe('ok')
  })

  it('reports a failed call as error', () => {
    expect(toolCallStatus(row({ result_json: { content: 'boom' }, is_error: true }))).toBe(
      'error',
    )
  })

  it('does not call an unfinished error row a failure', () => {
    // `is_error` defaults to false on the row, but a row that has not come back
    // is not a success either — the null result is what decides.
    expect(toolCallStatus(row({ result_json: null, is_error: true }))).toBe('running')
  })

  it('treats an explicit null JSON result as finished, not running', () => {
    // A tool that legitimately returned JSON `null` writes 0/false-y values, so
    // the check has to be for null/undefined rather than falsiness.
    expect(toolCallStatus(row({ result_json: 0 }))).toBe('ok')
    expect(toolCallStatus(row({ result_json: '' }))).toBe('ok')
    expect(toolCallStatus(row({ result_json: false }))).toBe('ok')
  })
})

function message(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: 1,
    session_id: 1,
    seq: 1,
    role: 'assistant',
    kind: 'assistant',
    content_json: [],
    text_preview: null,
    stop_reason: null,
    usage_json: null,
    created_at: '2026-09-14T12:00:00',
    tool_calls: [],
    ...overrides,
  }
}

/** What `search_feed_items` actually answers with, verbatim in shape. */
const INBOX_RESULT = [
  '1. [id 16] GitLab CVSS 10 File-Read Flaw Draws In-the-Wild Probes',
  '   The Hacker News · 2026-09-11 · https://thehackernews.com/2026/09/gitlab.html',
  '   GitLab has released patches to address multiple flaws…',
  '2. [id 20] Your Critical Vulnerabilities Might Not Be Your Biggest Risk',
  '   CISA advisories · 2026-09-11 · https://cisa.gov/news/risk',
  '   Security teams have become exceptionally talented…',
].join('\n')

describe('toolTag', () => {
  it('names the three sources the user thinks in', () => {
    expect(toolTag('builtin', 'search_feed_items')).toBe('local')
    expect(toolTag('server', 'web_search')).toBe('web')
    expect(toolTag('server', 'web_fetch')).toBe('web')
    expect(toolTag('mcp', 'mcp__files__read_text_file', 'files')).toBe('mcp · files')
  })

  it('recovers the MCP server from the namespaced name when the row has none', () => {
    // The SSE frame carries only `source`, so a live MCP call has no server
    // column to read — but the name it was registered under still says which.
    expect(toolTag('mcp', 'mcp__files__read_text_file', null)).toBe('mcp · files')
  })

  it('separates the code interpreter from the web tools it wraps', () => {
    expect(toolTag('server', 'code_execution')).toBe('code')
    expect(toolTag('server', 'bash_code_execution')).toBe('code')
    expect(toolTag('server', 'something_new')).toBe('server')
  })
})

describe('toolHint', () => {
  it('quotes a query and plainly shows a target', () => {
    expect(toolHint('search_feed_items', { q: 'CVE-2026-21887', limit: 20 })).toBe(
      '"CVE-2026-21887"',
    )
    expect(toolHint('fetch_article', { url: 'https://example.com/a', max_chars: 8000 })).toBe(
      'https://example.com/a',
    )
    expect(toolHint('get_feed_item', { item_id: 131 })).toBe('item #131')
  })

  it('collapses a multi-line argument to its first line', () => {
    expect(toolHint('code_execution', { code: 'import json\nprint(1)' })).toBe('import json')
  })

  it('says nothing when the arguments have not arrived yet', () => {
    expect(toolHint('search_feed_items', null)).toBe('')
  })
})

describe('stepsFromMessage', () => {
  it('keeps reasoning and tool calls in the order the model produced them', () => {
    // The audit rows are written in one batch and carry no sequence, so only
    // `content_json` knows that the model thought, called a tool, then thought
    // again. Reading the rows alone puts every thinking block first.
    const steps = stepsFromMessage(
      message({
        content_json: [
          { type: 'thinking', thinking: 'Check the inbox first.' },
          { type: 'text', text: 'Let me look.' },
          { type: 'tool_use', id: 'toolu_1', name: 'search_feed_items', input: { q: 'kev' } },
          { type: 'thinking', thinking: 'Now confirm on the web.' },
          { type: 'server_tool_use', id: 'srvtoolu_1', name: 'web_search', input: { query: 'kev' } },
        ],
        tool_calls: [
          row({ id: 9, tool_use_id: 'srvtoolu_1', name: 'web_search', source: 'server' }),
          row({ id: 8, tool_use_id: 'toolu_1', result_json: { content: 'three items' } }),
        ],
      }),
    )
    expect(steps.map((step) => [step.kind, step.name])).toEqual([
      ['thinking', 'Thinking'],
      ['tool', 'search_feed_items'],
      ['thinking', 'Thinking'],
      ['tool', 'web_search'],
    ])
  })

  it('still renders a tool_use whose audit row never got written', () => {
    // The turn died between persisting the message and persisting the rows.
    const steps = stepsFromMessage(
      message({
        content_json: [{ type: 'tool_use', id: 'toolu_1', name: 'get_feed_item', input: { item_id: 4 } }],
        tool_calls: [],
      }),
    )
    expect(steps).toHaveLength(1)
    expect(steps[0].status).toBe('running')
    expect(steps[0].hint).toBe('item #4')
  })
})

describe('groupTurns', () => {
  const turns = () =>
    groupTurns([
      message({
        id: 1,
        role: 'user',
        kind: 'user',
        content_json: [
          { type: 'text', text: 'What is in the inbox?' },
          {
            type: 'text',
            text: 'Attached feed items:\n- id 16 · GitLab flaw · https://thehackernews.com/a\nUse get_feed_item with one of these ids to read the full text.',
          },
        ],
      }),
      message({
        id: 2,
        content_json: [
          { type: 'text', text: 'Looking now.' },
          { type: 'tool_use', id: 'toolu_1', name: 'search_feed_items', input: { q: 'kev' } },
        ],
        tool_calls: [row({ id: 5, tool_use_id: 'toolu_1', result_json: { content: INBOX_RESULT } })],
      }),
      message({ id: 3, role: 'user', kind: 'tool_result', content_json: [{ type: 'tool_result' }] }),
      message({ id: 4, content_json: [{ type: 'text', text: 'Two items.' }], stop_reason: 'end_turn' }),
      message({ id: 5, role: 'user', kind: 'user', content_json: [{ type: 'text', text: 'And now?' }] }),
    ])

  it('regroups the flat alternation into one entry per question', () => {
    expect(turns().map((turn) => turn.question)).toEqual(['What is in the inbox?', 'And now?'])
  })

  it('keeps the attachment block out of the question but not out of the turn', () => {
    // The server resolves attachments into a second text block, so it is the
    // only record of them — and it must never be read out as the question.
    const [first] = turns()
    expect(first.question).toBe('What is in the inbox?')
    expect(first.attachments).toEqual([
      { id: 16, title: 'GitLab flaw', url: 'https://thehackernews.com/a' },
    ])
  })

  it('joins every assistant message in the turn into one answer', () => {
    expect(turns()[0].answer).toBe('Looking now.\n\nTwo items.')
  })

  it('drops tool_result messages, whose content is already on the rows', () => {
    expect(turns()[0].steps.map((step) => step.name)).toEqual(['search_feed_items'])
  })

  it('carries a refusal through, even though it stored no content', () => {
    const [turn] = groupTurns([
      message({ id: 1, role: 'user', kind: 'user', content_json: [{ type: 'text', text: 'Hi' }] }),
      message({
        id: 2,
        content_json: [],
        stop_reason: 'refusal',
        usage_json: { stop_details: { category: 'cyber', explanation: 'no' } },
      }),
    ])
    expect(turn.error).toEqual({ type: 'refusal', message: 'no', category: 'cyber' })
  })
})

describe('collectSources', () => {
  it('reads the inbox search result back into feed items', () => {
    const steps = stepsFromMessage(
      message({
        content_json: [{ type: 'tool_use', id: 'toolu_1', name: 'search_feed_items', input: { q: 'kev' } }],
        tool_calls: [row({ id: 5, tool_use_id: 'toolu_1', result_json: { content: INBOX_RESULT } })],
      }),
    )
    expect(collectSources(steps)).toEqual([
      {
        n: 1,
        title: 'GitLab CVSS 10 File-Read Flaw Draws In-the-Wild Probes',
        name: 'The Hacker News',
        url: 'https://thehackernews.com/2026/09/gitlab.html',
      },
      {
        n: 2,
        title: 'Your Critical Vulnerabilities Might Not Be Your Biggest Risk',
        name: 'CISA advisories',
        url: 'https://cisa.gov/news/risk',
      },
    ])
  })

  it('numbers web results by domain and never counts a page twice', () => {
    const steps = stepsFromMessage(
      message({
        content_json: [
          { type: 'server_tool_use', id: 's1', name: 'web_search', input: { query: 'kev' } },
          { type: 'tool_use', id: 't1', name: 'fetch_article', input: { url: 'https://www.talos.com/fmc/' } },
        ],
        tool_calls: [
          row({
            id: 1,
            tool_use_id: 's1',
            name: 'web_search',
            source: 'server',
            result_json: {
              type: 'web_search_tool_result',
              content: [
                { type: 'web_search_result', title: 'Talos on FMC', url: 'https://talos.com/fmc' },
              ],
            },
          }),
          row({
            id: 2,
            tool_use_id: 't1',
            name: 'fetch_article',
            result_json: { content: '# https://www.talos.com/fmc/\n\n# Talos on FMC\n\nbody' },
          }),
        ],
      }),
    )
    // `www.` and a trailing slash are not a different page.
    expect(collectSources(steps)).toEqual([
      { n: 1, title: 'Talos on FMC', name: 'talos.com', url: 'https://talos.com/fmc' },
    ])
  })

  it('does not credit a failed call with a source', () => {
    const steps = stepsFromMessage(
      message({
        content_json: [{ type: 'tool_use', id: 't1', name: 'fetch_article', input: { url: 'https://x.test/a' } }],
        tool_calls: [
          row({
            id: 1,
            tool_use_id: 't1',
            name: 'fetch_article',
            is_error: true,
            result_json: { content: '# https://x.test/a\n\nError: could not fetch' },
          }),
        ],
      }),
    )
    expect(collectSources(steps)).toEqual([])
  })
})

describe('citationFor', () => {
  const sources = [
    { n: 1, title: 'KEV entry', name: 'cisa.gov', url: 'https://www.cisa.gov/kev/' },
    { n: 2, title: 'No link', name: 'inbox', url: null },
  ]

  it('matches a link to its source across www and trailing slashes', () => {
    expect(citationFor(sources, 'https://cisa.gov/kev')?.n).toBe(1)
  })

  it('leaves a link that is not a source alone', () => {
    expect(citationFor(sources, 'https://example.com/other')).toBeNull()
    expect(citationFor(sources, undefined)).toBeNull()
  })

  it('never matches a source that has no URL of its own', () => {
    expect(citationFor(sources, '')).toBeNull()
  })
})

describe('formatting', () => {
  it('abbreviates token counts the way the header reads them', () => {
    expect(formatTokens(182)).toBe('182')
    expect(formatTokens(8108)).toBe('8.1k')
    expect(formatTokens(357342)).toBe('357k')
  })

  it('groups milliseconds without a comma', () => {
    expect(formatMs(412)).toBe('412 ms')
    expect(formatMs(2108)).toBe('2 108 ms')
  })
})

describe('whenLabel', () => {
  const now = new Date(2026, 8, 14, 12, 0, 0)

  it('names the last two days rather than dating them', () => {
    // Midday, because the stored timestamps are naive UTC and a late-evening
    // one crosses into the next day for anyone east of Greenwich.
    expect(whenLabel('2026-09-14T06:39:23', now)).toBe('today')
    expect(whenLabel('2026-09-13T12:00:00', now)).toBe('yesterday')
  })

  it('falls back to a date once the week is out', () => {
    expect(whenLabel('2026-09-01T09:00:00', now)).toBe('Sep 1')
  })

  it('survives a timestamp it cannot read', () => {
    expect(whenLabel('not a date', now)).toBe('')
  })
})
