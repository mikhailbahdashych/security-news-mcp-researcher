import { useState } from 'react'

export interface ToolCardState {
  toolUseId: string
  name: string
  source: string
  /** Raw `input_json_delta` fragments, concatenated. Only valid JSON once complete. */
  partialJson: string
  /** Present for server tools, whose input arrives whole rather than streamed. */
  input?: Record<string, unknown>
  /** The MCP server that owns the tool, when the transcript already knows it. */
  server?: string | null
  status: 'running' | 'ok' | 'error'
  preview?: string
  durationMs?: number
  /** web_search results, or the raw error object when the server tool failed. */
  results?: { title?: string | null; url?: string | null }[] | Record<string, unknown> | null
}

const SOURCE_LABEL: Record<string, string> = {
  builtin: 'local',
  server: 'web',
  mcp: 'mcp',
}

/** `mcp__{server}__{tool}` -> `server`. The SSE event carries only the source, but
 *  the namespaced name is enough to say which server answered. */
function mcpServerName(card: ToolCardState): string | null {
  if (card.server) return card.server
  if (card.source !== 'mcp') return null
  const match = /^mcp__([^_]+(?:_[^_]+)*?)__/.exec(card.name)
  return match ? match[1] : null
}

function prettyArguments(card: ToolCardState): string {
  if (card.input) {
    return JSON.stringify(card.input, null, 2)
  }
  try {
    return JSON.stringify(JSON.parse(card.partialJson || '{}'), null, 2)
  } catch {
    // Still streaming: the fragments are not valid JSON yet, so show them raw.
    return card.partialJson
  }
}

function isResultList(
  results: ToolCardState['results'],
): results is { title?: string | null; url?: string | null }[] {
  return Array.isArray(results)
}

export default function ToolCallCard({ card }: { card: ToolCardState }) {
  const [open, setOpen] = useState(false)
  const mark = card.status === 'running' ? '⋯' : card.status === 'ok' ? '✓' : '✗'
  const server = mcpServerName(card)

  return (
    <div className="rounded-md border border-slate-200 bg-white">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs"
      >
        <span className={`transition-transform text-slate-400 ${open ? 'rotate-90' : ''}`}>›</span>
        <span className="font-mono font-medium text-slate-800">{card.name}</span>
        <span className="rounded border border-slate-200 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-slate-500">
          {SOURCE_LABEL[card.source] ?? card.source}
          {server ? ` · ${server}` : ''}
        </span>
        <span
          className={
            card.status === 'error'
              ? 'text-rose-600'
              : card.status === 'ok'
                ? 'text-emerald-600'
                : 'animate-pulse text-slate-400'
          }
        >
          {mark}
        </span>
        {card.durationMs !== undefined ? (
          <span className="ml-auto text-[10px] text-slate-400">{card.durationMs} ms</span>
        ) : null}
      </button>

      {open ? (
        <div className="space-y-2 border-t border-slate-100 px-3 py-2">
          <pre className="max-h-40 overflow-auto rounded bg-slate-50 p-2 text-[11px] leading-relaxed text-slate-700">
            {prettyArguments(card)}
          </pre>

          {isResultList(card.results) ? (
            <ul className="space-y-1 text-xs">
              {card.results.map((result, index) => (
                <li key={`${result.url ?? index}`}>
                  {result.url ? (
                    <a
                      href={result.url}
                      target="_blank"
                      rel="noreferrer noopener"
                      className="text-sky-700 underline underline-offset-2"
                    >
                      {result.title || result.url}
                    </a>
                  ) : (
                    <span className="text-slate-600">{result.title}</span>
                  )}
                </li>
              ))}
            </ul>
          ) : null}

          {card.results && !isResultList(card.results) ? (
            <pre className="overflow-auto rounded bg-rose-50 p-2 text-[11px] text-rose-700">
              {JSON.stringify(card.results, null, 2)}
            </pre>
          ) : null}

          {card.preview ? (
            <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded bg-slate-50 p-2 text-[11px] leading-relaxed text-slate-700">
              {card.preview}
            </pre>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
