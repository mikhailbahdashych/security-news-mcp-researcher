import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import type { ErrorPayload } from '../../api/chat'
import { fetchSessions, sessionsQueryKey } from '../../api/chat'
import { fetchItems, type FeedItem } from '../../api/inbox'
import {
  cancelGeneration,
  generateUrl,
  notesQueryKey,
  type GenerateNotesBody,
  type NoteDonePayload,
} from '../../api/notes'
import { fetchSettings, settingsQueryKey } from '../../api/settings'
import { SSEHttpError, streamSSE } from '../../lib/sse'
import TurnError from '../chat/TurnError'
import useDebouncedValue from '../inbox/useDebouncedValue'

export interface GenerateNotesDialogProps {
  /** Items the caller already has in hand (the Inbox selection), pinned at the top. */
  initialItems?: FeedItem[]
  /** A research session to generate from (the Chat header's action). */
  initialSessionId?: number | null
  onClose: () => void
}

const primaryButtonClass =
  'rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800 ' +
  'disabled:bg-slate-300'

const secondaryButtonClass =
  'rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 ' +
  'hover:border-slate-400 disabled:opacity-40'

/** What a running tool looks like in the status line. */
const ACTIVITY: Record<string, string> = {
  fetch_article: 'Fetching an article…',
  get_feed_item: 'Reading an inbox item…',
  web_search: 'Searching the web…',
}

/**
 * Pick sources, then watch the note being written.
 *
 * Opened from three places — the Notes page, the Inbox selection bar and a chat
 * session — so everything it needs arrives as props rather than being read off
 * the route.
 */
export default function GenerateNotesDialog({
  initialItems = [],
  initialSessionId = null,
  onClose,
}: GenerateNotesDialogProps) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [selected, setSelected] = useState<Map<number, FeedItem>>(
    () => new Map(initialItems.map((item) => [item.id, item])),
  )
  const [sessionId, setSessionId] = useState<number | null>(initialSessionId)
  const [title, setTitle] = useState('')
  const [search, setSearch] = useState('')
  const [advanced, setAdvanced] = useState(false)
  const [templateOverride, setTemplateOverride] = useState<string | null>(null)

  const [streaming, setStreaming] = useState(false)
  const [preview, setPreview] = useState('')
  const [activity, setActivity] = useState<string | null>(null)
  const [error, setError] = useState<ErrorPayload | null>(null)

  const abort = useRef<AbortController | null>(null)
  const generationId = useRef<string | null>(null)

  const debouncedSearch = useDebouncedValue(search)

  const items = useQuery({
    queryKey: ['notes-picker-items', debouncedSearch],
    queryFn: () =>
      fetchItems({
        // Blank search means "the starred inbox", which is what notes are for.
        status: debouncedSearch.trim() ? 'all' : 'starred',
        feedId: null,
        q: debouncedSearch,
      }),
  })

  const sessions = useQuery({ queryKey: sessionsQueryKey, queryFn: () => fetchSessions() })
  const settings = useQuery({ queryKey: settingsQueryKey, queryFn: fetchSettings })

  // Selected items always render, even when the current filter excludes them —
  // otherwise an Inbox selection would silently vanish behind a search.
  const listed = useMemo(() => {
    const fetched = (items.data?.items ?? []).filter((item) => !selected.has(item.id))
    return [...selected.values(), ...fetched]
  }, [items.data, selected])

  const toggle = (item: FeedItem, checked: boolean) => {
    setSelected((current) => {
      const next = new Map(current)
      if (checked) {
        next.set(item.id, item)
      } else {
        next.delete(item.id)
      }
      return next
    })
  }

  const canSubmit = (selected.size > 0 || sessionId !== null) && !streaming

  const submit = useCallback(async () => {
    const body: GenerateNotesBody = {
      item_ids: [...selected.keys()],
      session_id: sessionId,
      generation_id: crypto.randomUUID(),
    }
    if (title.trim()) {
      body.title = title.trim()
    }
    if (templateOverride !== null && templateOverride.trim()) {
      body.template_override = templateOverride
    }

    generationId.current = body.generation_id
    const controller = new AbortController()
    abort.current = controller

    setStreaming(true)
    setPreview('')
    setActivity(null)
    setError(null)

    let savedNoteId: number | null = null
    let failure: ErrorPayload | null = null

    try {
      await streamSSE({
        url: generateUrl(),
        body,
        signal: controller.signal,
        onEvent: ({ event, data }) => {
          let payload: unknown
          try {
            payload = JSON.parse(data)
          } catch {
            return
          }
          switch (event) {
            case 'turn_start': {
              const id = (payload as { generation_id?: string }).generation_id
              if (id) {
                generationId.current = id
              }
              return
            }
            case 'text_delta':
              // Appended as plain text while streaming: re-parsing Markdown on
              // every token flickers far more than it helps.
              setPreview((current) => current + (payload as { text: string }).text)
              return
            case 'tool_use_start':
            case 'server_tool_use': {
              const name = (payload as { name: string }).name
              setActivity(ACTIVITY[name] ?? `Running ${name}…`)
              return
            }
            case 'tool_result':
            case 'server_tool_result':
              setActivity(null)
              return
            case 'error':
              failure = payload as ErrorPayload
              return
            case 'done':
              savedNoteId = (payload as NoteDonePayload).note_id
              return
            default:
          }
        },
      })
    } catch (streamError) {
      failure = controller.signal.aborted
        ? { type: 'cancelled', message: 'Stopped.', category: null }
        : {
            type: streamError instanceof SSEHttpError ? 'api_error' : 'connection',
            message: streamError instanceof Error ? streamError.message : String(streamError),
            category: null,
          }
    } finally {
      abort.current = null
      setStreaming(false)
      setActivity(null)
    }

    if (savedNoteId !== null) {
      await queryClient.invalidateQueries({ queryKey: notesQueryKey })
      navigate(`/notes/${savedNoteId}`)
      onClose()
      return
    }
    // The selection is deliberately left intact so the user can retry, adjust
    // the items, or copy the half-written preview out.
    setError(
      failure ?? {
        type: 'api_error',
        message: 'The generation ended without saving a note.',
        category: null,
      },
    )
  }, [navigate, onClose, queryClient, selected, sessionId, templateOverride, title])

  const stop = useCallback(() => {
    // Both halves: the abort stops the browser reading, the endpoint stops the
    // server billing. An SSE disconnect alone does neither reliably.
    abort.current?.abort()
    const id = generationId.current
    if (id) {
      void cancelGeneration(id).catch(() => undefined)
    }
  }, [])

  const template = templateOverride ?? settings.data?.note_template ?? ''

  return (
    <div className="fixed inset-0 z-20 flex items-start justify-center overflow-y-auto bg-slate-900/30 p-6">
      <div className="w-full max-w-3xl rounded-lg border border-slate-200 bg-white shadow-lg">
        <header className="flex items-center justify-between border-b border-slate-200 px-5 py-3">
          <div>
            <h2 className="text-sm font-semibold text-slate-900">Generate meeting notes</h2>
            <p className="text-xs text-slate-500">
              Pick the news items, a research session, or both.
            </p>
          </div>
          <button
            type="button"
            onClick={() => {
              stop()
              onClose()
            }}
            className="text-slate-400 hover:text-slate-900"
            aria-label="Close"
          >
            ✕
          </button>
        </header>

        <div className="max-h-[70vh] space-y-4 overflow-y-auto px-5 py-4">
          <section>
            <div className="flex items-center justify-between gap-3">
              <h3 className="text-xs font-medium text-slate-700">
                News items{selected.size > 0 ? ` · ${selected.size} selected` : ''}
              </h3>
              <input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Search the inbox (blank shows starred)"
                className="w-64 rounded-md border border-slate-300 px-2 py-1 text-xs"
              />
            </div>

            <ul className="mt-2 max-h-56 space-y-0.5 overflow-y-auto rounded-md border border-slate-200 p-1.5">
              {items.isPending ? (
                <li className="px-2 py-3 text-xs text-slate-500">Loading items…</li>
              ) : null}
              {!items.isPending && listed.length === 0 ? (
                <li className="px-2 py-3 text-xs text-slate-500">
                  No starred items. Star a few in the Inbox first, or search for them here.
                </li>
              ) : null}
              {listed.map((item) => (
                <li key={item.id}>
                  <label className="flex cursor-pointer items-start gap-2 rounded px-2 py-1.5 hover:bg-slate-50">
                    <input
                      type="checkbox"
                      checked={selected.has(item.id)}
                      onChange={(event) => toggle(item, event.target.checked)}
                      className="mt-0.5 size-3.5 shrink-0 rounded border-slate-300 accent-slate-900"
                    />
                    <span className="min-w-0">
                      <span className="block truncate text-xs text-slate-800">{item.title}</span>
                      <span className="block truncate text-[11px] text-slate-500">
                        {item.feed_title ?? 'Unknown feed'}
                      </span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          </section>

          <section className="grid gap-3 sm:grid-cols-2">
            <label className="flex flex-col gap-1.5">
              <span className="text-xs font-medium text-slate-700">Research session (optional)</span>
              <select
                value={sessionId ?? ''}
                onChange={(event) =>
                  setSessionId(event.target.value ? Number(event.target.value) : null)
                }
                className="rounded-md border border-slate-300 px-2 py-1.5 text-xs"
              >
                <option value="">None</option>
                {(sessions.data?.sessions ?? []).map((session) => (
                  <option key={session.id} value={session.id}>
                    {session.title ?? `Session ${session.id}`}
                  </option>
                ))}
              </select>
            </label>

            <label className="flex flex-col gap-1.5">
              <span className="text-xs font-medium text-slate-700">Title (optional)</span>
              <input
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                placeholder="Defaults to the item title, or a dated one"
                className="rounded-md border border-slate-300 px-2 py-1.5 text-xs"
              />
            </label>
          </section>

          <section>
            <button
              type="button"
              onClick={() => setAdvanced((value) => !value)}
              className="text-xs font-medium text-slate-500 hover:text-slate-900"
            >
              {advanced ? '▾' : '▸'} Advanced — template for this note
            </button>
            {advanced ? (
              <div className="mt-2 space-y-1.5">
                <textarea
                  rows={7}
                  value={template}
                  onChange={(event) => setTemplateOverride(event.target.value)}
                  className="w-full rounded-md border border-slate-300 px-2 py-1.5 font-mono text-xs"
                />
                <p className="text-[11px] text-slate-500">
                  Used for this note only. Editing the default for every note is in Settings.
                </p>
              </div>
            ) : null}
          </section>

          {streaming || preview || error ? (
            <section className="space-y-2">
              {activity ? (
                <p className="text-xs text-slate-500">
                  <span className="animate-pulse">⋯</span> {activity}
                </p>
              ) : null}
              {preview ? (
                <pre className="max-h-64 overflow-y-auto rounded-md border border-slate-200 bg-slate-50 p-3 text-[11px] leading-relaxed whitespace-pre-wrap text-slate-700">
                  {preview}
                </pre>
              ) : streaming ? (
                <p className="text-xs text-slate-500">Thinking…</p>
              ) : null}
              {error ? <TurnError error={error} /> : null}
            </section>
          ) : null}
        </div>

        <footer className="flex items-center justify-end gap-2 border-t border-slate-200 px-5 py-3">
          {streaming ? (
            <button type="button" onClick={stop} className={secondaryButtonClass}>
              Stop
            </button>
          ) : (
            <button type="button" onClick={onClose} className={secondaryButtonClass}>
              Cancel
            </button>
          )}
          <button
            type="button"
            disabled={!canSubmit}
            onClick={() => void submit()}
            className={primaryButtonClass}
          >
            {streaming ? 'Generating…' : 'Generate'}
          </button>
        </footer>
      </div>
    </div>
  )
}
