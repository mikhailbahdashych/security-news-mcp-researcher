import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'

import {
  cancelTurn,
  collectFeedTitles,
  createSession,
  deleteSession,
  fetchSession,
  fetchSessions,
  formatTokens,
  groupTurns,
  messagesUrl,
  renameSession,
  sessionQueryKey,
  sessionsListKey,
  sessionsQueryKey,
  setSessionArchived,
  type ErrorPayload,
  type SessionFilters,
} from '../api/chat'
import { useFeedTitlesFromCache, type FeedItem } from '../api/inbox'
import { fetchSettings, settingsQueryKey } from '../api/settings'
import AnswerTurn from '../components/chat/AnswerTurn'
import Composer from '../components/chat/Composer'
import EmptyResearch from '../components/chat/EmptyResearch'
import HistoryDrawer from '../components/chat/HistoryDrawer'
import TurnError from '../components/chat/TurnError'
import {
  emptyTurn,
  isForeignSession,
  liveSteps,
  liveTurnReducer,
} from '../components/chat/liveTurn'
import GenerateNotesDialog from '../components/notes/GenerateNotesDialog'
import Button from '../components/ui/Button'
import IconButton from '../components/ui/IconButton'
import type { EmbeddablePageProps } from '../components/ui/PageHost'
import { SSEHttpError, streamSSE } from '../lib/sse'
import useDebouncedValue from '../lib/useDebouncedValue'

/** What the Inbox's "Research these" button hands over. */
export interface ChatNavigationState {
  attachedItems?: FeedItem[]
}

export default function ChatPage({ embedded = false }: EmbeddablePageProps) {
  const params = useParams<{ id?: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const queryClient = useQueryClient()

  // Which conversation is open. Routed, that is the URL; embedded (the split
  // view's right pane) the URL belongs to the other pane, so it is local state
  // and every "open this session" goes through `openSession` instead.
  const [embeddedSessionId, setEmbeddedSessionId] = useState<number | null>(null)
  const sessionId = embedded ? embeddedSessionId : params.id ? Number(params.id) : null

  const openSession = useCallback(
    (id: number | null, replace = false) => {
      if (embedded) {
        setEmbeddedSessionId(id)
        return
      }
      navigate(id === null ? '/chat' : `/chat/${id}`, { replace })
    },
    [embedded, navigate],
  )

  const [live, dispatch] = useReducer(liveTurnReducer, emptyTurn)
  // Items handed over by the Inbox's "Research these" ride in on route state, so
  // they are the initial value rather than something an effect sets afterwards.
  // The embedded pane is not the one that was navigated to, so it takes none.
  const [attached, setAttached] = useState<FeedItem[]>(() =>
    embedded ? [] : ((location.state as ChatNavigationState | null)?.attachedItems ?? []),
  )
  const [notesOpen, setNotesOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [sessionSearch, setSessionSearch] = useState('')
  const [showArchived, setShowArchived] = useState(false)
  const abort = useRef<AbortController | null>(null)
  const scroller = useRef<HTMLDivElement>(null)
  const bottom = useRef<HTMLDivElement>(null)

  const debouncedSessionSearch = useDebouncedValue(sessionSearch)
  const sessionFilters = useMemo<SessionFilters>(
    () => ({ q: debouncedSessionSearch, archived: showArchived ? 'true' : 'false' }),
    [debouncedSessionSearch, showArchived],
  )

  const sessions = useInfiniteQuery({
    // Keyed by the filters, prefixed by `sessionsQueryKey` so one invalidation
    // after a rename or an archive still refreshes whichever variant is on
    // screen.
    queryKey: sessionsListKey(sessionFilters),
    queryFn: ({ pageParam }) => fetchSessions(sessionFilters, pageParam as string | undefined),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    // The drawer is an overlay; there is no point paying for the list while it
    // is shut, and opening it is instant off the cache afterwards.
    enabled: historyOpen,
  })

  const detail = useQuery({
    queryKey: sessionQueryKey(sessionId ?? 0),
    queryFn: () => fetchSession(sessionId as number),
    enabled: sessionId !== null,
  })

  // The configured model, for the composer's "what will answer this" line. The
  // session's own model is authoritative once there is one.
  const settings = useQuery({ queryKey: settingsQueryKey, queryFn: fetchSettings })

  // Leaving for a *different* conversation drops the live turn — this is what
  // catches browser back/forward, which no click handler sees. Only a route that
  // names another session counts: react-router 7 runs `BrowserRouter`'s location
  // update inside `React.startTransition`, so the urgent `start` dispatch
  // renders before the navigation lands and the route is still `/chat`, with no
  // id at all. Reading that as "the user left" reset every turn started from the
  // empty view on its first render.
  const staleSession = isForeignSession(live, sessionId)
  useEffect(() => {
    if (staleSession) {
      dispatch({ kind: 'reset' })
    }
  }, [staleSession])

  // Clear the handover off the history entry so a reload does not re-attach.
  useEffect(() => {
    if (!embedded && (location.state as ChatNavigationState | null)?.attachedItems) {
      navigate(location.pathname, { replace: true, state: null })
    }
  }, [embedded, location.pathname, location.state, navigate])

  const messages = detail.data?.messages
  // One lookup of "which feed is item N from", built once and handed to both the
  // transcript and the live turn. The transcript states it for items the model
  // searched for; the cache — the Inbox list, the attachment picker, the note
  // generator — adds the ones it only opened, so a source card never falls back
  // to a bare domain for an item the app can already name.
  const cachedTitles = useFeedTitlesFromCache()
  const feedTitles = useMemo(
    () => collectFeedTitles(messages ?? [], cachedTitles),
    [messages, cachedTitles],
  )
  const turns = useMemo(() => groupTurns(messages ?? [], feedTitles), [messages, feedTitles])

  // Opening a conversation lands at its latest answer, without animating
  // through the whole history to get there.
  useEffect(() => {
    const element = scroller.current
    if (element) {
      element.scrollTop = element.scrollHeight
    }
  }, [sessionId, turns.length])

  // While a turn is running, follow it — once a frame, and without animating.
  // Deltas arrive many times a second, and each one restarted a smooth scroll
  // that never had time to finish: the column crawled behind the text.
  useEffect(() => {
    if (live.prompt === null) {
      return
    }
    const frame = requestAnimationFrame(() => {
      bottom.current?.scrollIntoView({ behavior: 'auto', block: 'end' })
    })
    return () => cancelAnimationFrame(frame)
  }, [live.prompt, live.text, live.steps.length])

  const rename = useMutation({
    mutationFn: ({ id, title }: { id: number; title: string }) => renameSession(id, title),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: sessionsQueryKey }),
  })

  const archive = useMutation({
    mutationFn: ({ id, archived }: { id: number; archived: boolean }) =>
      setSessionArchived(id, archived),
    // Invalidate rather than patch the cache: an archived row leaves the default
    // list entirely, which is not an edit to a row but a change of membership.
    onSuccess: () => queryClient.invalidateQueries({ queryKey: sessionsQueryKey }),
  })

  const remove = useMutation({
    mutationFn: (id: number) => deleteSession(id),
    onSuccess: (_data, id) => {
      queryClient.invalidateQueries({ queryKey: sessionsQueryKey })
      if (id === sessionId) {
        // Without this the deleted session's terminal error would follow the
        // user onto the blank /chat view.
        dispatch({ kind: 'reset' })
        openSession(null)
      }
    },
  })

  const send = useCallback(
    async (text: string) => {
      let id = sessionId
      if (id === null) {
        const created = await createSession({})
        id = created.id
        await queryClient.invalidateQueries({ queryKey: sessionsQueryKey })
        openSession(id, true)
      }

      const itemIds = attached.map((item) => item.id)
      setAttached([])
      dispatch({ kind: 'start', prompt: text, sessionId: id })

      const controller = new AbortController()
      abort.current = controller

      try {
        await streamSSE({
          url: messagesUrl(id),
          body: { content: text, attached_item_ids: itemIds },
          signal: controller.signal,
          onEvent: ({ event, data }) => {
            let payload: unknown = null
            try {
              payload = JSON.parse(data)
            } catch {
              return
            }
            dispatch({ kind: 'sse', event, payload })
          },
        })
      } catch (error) {
        if (controller.signal.aborted) {
          dispatch({
            kind: 'failed',
            error: { type: 'cancelled', message: 'Stopped.', category: null },
          })
        } else {
          const payload: ErrorPayload = {
            type: error instanceof SSEHttpError && error.status === 409 ? 'api_error' : 'connection',
            message: error instanceof Error ? error.message : String(error),
            category: null,
          }
          dispatch({ kind: 'failed', error: payload })
        }
      } finally {
        abort.current = null
        // The Query cache becomes the source of truth again once the turn ends.
        await queryClient.invalidateQueries({ queryKey: sessionQueryKey(id) })
        await queryClient.invalidateQueries({ queryKey: sessionsQueryKey })
        // `settle`, not `reset`: a refusal, a stop or a connection failure has to
        // stay on screen until the next send, or the user is left looking at
        // their own question with nothing under it.
        dispatch({ kind: 'settle' })
      }
    },
    [attached, openSession, queryClient, sessionId],
  )

  const stop = useCallback(() => {
    // Both halves are needed: the abort stops the browser reading, and the
    // endpoint stops the server billing. An SSE disconnect alone does neither.
    abort.current?.abort()
    if (sessionId !== null) {
      void cancelTurn(sessionId).catch(() => undefined)
    }
  }, [sessionId])

  const newChat = useCallback(() => {
    dispatch({ kind: 'reset' })
    setAttached([])
    setHistoryOpen(false)
    openSession(null)
  }, [openSession])

  const allSessions = sessions.data?.pages.flatMap((page) => page.sessions) ?? []
  // Which row is mid-write, so the drawer can grey it out while it saves.
  const busyId =
    (rename.isPending ? rename.variables?.id : undefined) ??
    (archive.isPending ? archive.variables?.id : undefined) ??
    (remove.isPending ? remove.variables : undefined) ??
    null

  const session = detail.data?.session
  const model = session?.model ?? settings.data?.model ?? null
  // Keyed on what the steps are actually derived from, not on `live`: the turn
  // object is replaced on every delta, so a memo on it re-parsed every step's
  // JSON, links and sources for every chunk of streamed text.
  const answered = live.text !== ''
  const steps = useMemo(
    () => liveSteps(live.steps, { streaming: live.streaming, answered, feedTitles }),
    [live.steps, live.streaming, answered, feedTitles],
  )
  // `settle` clears the prompt but keeps a terminal error, so once the stream is
  // over the notice belongs under the last answer — not in a turn of its own
  // with a divider above it and no question to explain it.
  const settledError = live.prompt === null ? live.error : null
  const liveTurnVisible = live.prompt !== null
  const empty = turns.length === 0 && !liveTurnVisible && settledError === null

  const headerMeta =
    session && model
      ? `${model} · ${formatTokens(session.total_input_tokens)} in / ${formatTokens(session.total_output_tokens)} out`
      : ''

  const attachProps = {
    attached,
    onAttach: (item: FeedItem) =>
      setAttached((current) =>
        current.some((existing) => existing.id === item.id) ? current : [...current, item],
      ),
    onDetach: (id: number) => setAttached((current) => current.filter((item) => item.id !== id)),
    onSend: (text: string) => void send(text),
    onStop: stop,
  }

  return (
    <section className="relative flex h-full flex-col overflow-hidden bg-bg text-ink">
      <header className="flex h-[49px] shrink-0 items-center gap-2 border-b border-line px-4">
        <IconButton
          icon="history"
          label="Chat history"
          size={16}
          active={historyOpen}
          onClick={() => setHistoryOpen((open) => !open)}
        />
        <div className="flex min-w-0 flex-1 items-baseline gap-2 overflow-hidden">
          <h1 className="m-0 min-w-0 shrink truncate text-[13px] font-semibold">
            {session?.title || 'New research'}
          </h1>
          {/* Shrinks four times faster than the title: in a narrow split pane
              the name of the chat is worth more than its token count. */}
          {headerMeta ? (
            <span className="min-w-0 shrink-4 truncate text-[11px] text-faint">{headerMeta}</span>
          ) : null}
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {sessionId !== null ? (
            <Button size="sm" disabled={live.streaming} onClick={() => setNotesOpen(true)}>
              Generate notes
            </Button>
          ) : null}
          <Button size="sm" onClick={newChat}>
            New chat
          </Button>
        </div>
      </header>

      {historyOpen ? (
        <HistoryDrawer
          sessions={allSessions}
          activeId={sessionId}
          search={sessionSearch}
          showArchived={showArchived}
          isPending={sessions.isPending}
          isError={sessions.isError}
          hasMore={Boolean(sessions.hasNextPage)}
          loadingMore={sessions.isFetchingNextPage}
          busyId={busyId}
          onSearchChange={setSessionSearch}
          onShowArchivedChange={setShowArchived}
          onOpen={(id) => {
            dispatch({ kind: 'reset' })
            setHistoryOpen(false)
            openSession(id)
          }}
          onRename={(id, title) => rename.mutate({ id, title })}
          onArchive={(id, archived) => archive.mutate({ id, archived })}
          onDelete={(id) => remove.mutateAsync(id).then(() => undefined)}
          onLoadMore={() => void sessions.fetchNextPage()}
          onClose={() => setHistoryOpen(false)}
        />
      ) : null}

      {empty ? (
        <EmptyResearch
          streaming={live.streaming}
          embedded={embedded}
          meta={model ? `${model} · inbox first, then web` : undefined}
          {...attachProps}
        />
      ) : (
        <>
          <div ref={scroller} className="flex-1 overflow-y-auto">
            <div className="mx-auto flex max-w-[720px] flex-col gap-3.5 px-6 pt-6 pb-4">
              {turns.map((turn, index) => (
                <AnswerTurn
                  key={turn.key}
                  question={turn.question}
                  attachments={turn.attachments}
                  steps={turn.steps}
                  answer={turn.answer}
                  error={turn.error}
                  followUp={index > 0}
                />
              ))}

              {liveTurnVisible ? (
                <AnswerTurn
                  question={live.prompt ?? ''}
                  attachments={[]}
                  steps={steps}
                  answer={live.text}
                  error={live.error}
                  followUp={turns.length > 0}
                  streaming={live.streaming}
                />
              ) : null}

              {settledError ? <TurnError error={settledError} /> : null}

              <div ref={bottom} />
            </div>
          </div>

          <div className="shrink-0 px-6 pt-2.5 pb-4">
            <div className="mx-auto max-w-[720px]">
              <Composer variant="bar" streaming={live.streaming} {...attachProps} />
            </div>
          </div>
        </>
      )}

      {notesOpen && sessionId !== null ? (
        <GenerateNotesDialog initialSessionId={sessionId} onClose={() => setNotesOpen(false)} />
      ) : null}
    </section>
  )
}
