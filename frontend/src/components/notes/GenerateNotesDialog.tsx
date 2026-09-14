import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import type { ErrorPayload } from '../../api/chat'
import { fetchSessions } from '../../api/chat'
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
import Button from '../ui/Button'
import Dialog from '../ui/Dialog'
import Icon from '../ui/Icon'
import Input from '../ui/Input'
import Select from '../ui/Select'
import Textarea from '../ui/Textarea'
import { FIELD_HINT, FIELD_LABEL, cx } from '../ui/classes'

export interface GenerateNotesDialogProps {
  /** Items the caller already has in hand (the Inbox selection), pinned at the top. */
  initialItems?: FeedItem[]
  /** A research session to generate from (the Chat header's action). */
  initialSessionId?: number | null
  onClose: () => void
  /**
   * What to do with the saved note instead of navigating to `/notes/:id`.
   *
   * Only the embedded Notes pane passes this: it has no route to navigate, so it
   * opens the note in place.
   */
  onGenerated?: (noteId: number) => void
}

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
  onGenerated,
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

  // Its own key on purpose: the Chat sidebar caches `sessionsQueryKey` as an
  // *infinite* query, and a plain useQuery sharing that key reads back
  // `{pages: [...]}` — the dropdown would silently come up empty, and whichever
  // of the two loaded second would find the wrong shape in the cache.
  const sessions = useQuery({ queryKey: ['notes-picker-sessions'], queryFn: () => fetchSessions() })
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
      if (onGenerated) {
        onGenerated(savedNoteId)
      } else {
        navigate(`/notes/${savedNoteId}`)
      }
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
  }, [navigate, onClose, onGenerated, queryClient, selected, sessionId, templateOverride, title])

  const stop = useCallback(() => {
    // Both halves: the abort stops the browser reading, the endpoint stops the
    // server billing. An SSE disconnect alone does neither reliably.
    abort.current?.abort()
    const id = generationId.current
    if (id) {
      void cancelGeneration(id).catch(() => undefined)
    }
  }, [])

  // Every way out of the dialog — ✕, Escape, the backdrop, Cancel — also stops a
  // generation that is still running, so closing never leaves one billing.
  const close = useCallback(() => {
    stop()
    onClose()
  }, [onClose, stop])

  const template = templateOverride ?? settings.data?.note_template ?? ''

  return (
    <Dialog
      title="Generate meeting notes"
      description="Pick the news items, a research session, or both."
      width="lg"
      onClose={close}
      footer={
        <>
          {streaming ? (
            <Button onClick={stop}>Stop</Button>
          ) : (
            <Button onClick={close}>Cancel</Button>
          )}
          <Button variant="primary" disabled={!canSubmit} loading={streaming} onClick={() => void submit()}>
            {streaming ? 'Generating…' : 'Generate'}
          </Button>
        </>
      }
    >
      <div className="flex max-h-[60vh] flex-col gap-4 overflow-y-auto">
        <section>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h3 className={FIELD_LABEL}>
              News items{selected.size > 0 ? ` · ${selected.size} selected` : ''}
            </h3>
            <Input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search the inbox (blank shows starred)"
              aria-label="Search the inbox"
              tone="bg"
              className="w-[280px] max-w-full"
            />
          </div>

          <ul className="mt-2 max-h-56 overflow-y-auto rounded-[8px] border border-line bg-bg p-1.5">
            {items.isPending ? (
              <li className="px-2 py-3 text-[12px] text-muted">Loading items…</li>
            ) : null}
            {!items.isPending && listed.length === 0 ? (
              <li className="px-2 py-3 text-[12px] text-muted">
                No starred items. Star a few in the Inbox first, or search for them here.
              </li>
            ) : null}
            {listed.map((item) => (
              <li key={item.id}>
                <label className="flex cursor-pointer items-start gap-2.5 rounded-[6px] px-2 py-1.5 transition-colors duration-150 hover:bg-hover">
                  <input
                    type="checkbox"
                    checked={selected.has(item.id)}
                    onChange={(event) => toggle(item, event.target.checked)}
                    className="mt-0.5 size-[14px] shrink-0 accent-[var(--accent-btn)]"
                  />
                  <span className="min-w-0">
                    <span className="block truncate text-[12px] text-ink">{item.title}</span>
                    <span className="block truncate text-[11px] text-faint">
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
            <span className={FIELD_LABEL}>Research session (optional)</span>
            <Select
              tone="bg"
              value={sessionId ?? ''}
              onChange={(event) =>
                setSessionId(event.target.value ? Number(event.target.value) : null)
              }
            >
              <option value="">None</option>
              {(sessions.data?.sessions ?? []).map((session) => (
                <option key={session.id} value={session.id}>
                  {session.title ?? `Session ${session.id}`}
                </option>
              ))}
            </Select>
          </label>

          <label className="flex flex-col gap-1.5">
            <span className={FIELD_LABEL}>Title (optional)</span>
            <Input
              tone="bg"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="Defaults to the item title, or a dated one"
            />
          </label>
        </section>

        <section>
          <button
            type="button"
            onClick={() => setAdvanced((value) => !value)}
            aria-expanded={advanced}
            className={cx(
              'flex items-center gap-1.5 text-[12px] font-medium text-muted',
              'transition-colors duration-150 hover:text-ink',
            )}
          >
            <Icon name={advanced ? 'chevronDown' : 'chevronRight'} size={14} />
            Template for this note
          </button>
          {advanced ? (
            <div className="mt-2 flex flex-col gap-1.5">
              <Textarea
                rows={7}
                mono
                tone="bg"
                value={template}
                aria-label="Template for this note"
                onChange={(event) => setTemplateOverride(event.target.value)}
              />
              <p className={FIELD_HINT}>
                Used for this note only. Editing the default for every note is in Settings.
              </p>
            </div>
          ) : null}
        </section>

        {streaming || preview || error ? (
          <section className="flex flex-col gap-2">
            {activity ? (
              <p className="flex items-center gap-1.5 text-[12px] text-muted">
                <Icon name="spinner" size={13} />
                {activity}
              </p>
            ) : null}
            {preview ? (
              <pre className="max-h-64 overflow-y-auto rounded-[8px] border border-line bg-code p-3 font-mono text-[11.5px] leading-[1.6] whitespace-pre-wrap text-muted">
                {preview}
              </pre>
            ) : streaming ? (
              <p className="flex items-center gap-1.5 text-[12px] text-muted">
                <Icon name="spinner" size={13} />
                Thinking…
              </p>
            ) : null}
            {error?.type === 'cancelled' ? (
              // Not TurnError's chat copy: a stopped chat turn keeps what it
              // wrote, and a stopped generation keeps nothing at all.
              <p className="rounded-[8px] border border-line bg-panel2 px-3 py-2 text-[12px] text-muted">
                Stopped. Nothing was saved — the preview above is all there was.
              </p>
            ) : error ? (
              <TurnError error={error} />
            ) : null}
          </section>
        ) : null}
      </div>
    </Dialog>
  )
}
