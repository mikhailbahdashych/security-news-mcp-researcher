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
  parseSessionId,
  renameSession,
  sessionQueryKey,
  sessionsListKey,
  sessionsQueryKey,
  setSessionArchived,
  type ErrorPayload,
  type SessionFilters,
} from '../api/chat'
import { isNotFound } from '../api/client'
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
  turnsBesideLive,
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
  // `parseSessionId`, not `Number()`: the router matches `:id` against anything,
  // and `Number('abc')` is `NaN`, `Number('1.5')` is `1.5`. Those went to the API
  // and came back 422, which the 404 path below cannot act on. Null here keeps
  // the detail query disabled, so the bad request is never made at all.
  const routeSessionId = parseSessionId(params.id)
  const sessionId = embedded ? embeddedSessionId : routeSessionId

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
  // Which send owns the live state. Bumped whenever a turn is abandoned, so a
  // reader that is still running cannot write to the turn that replaced it.
  const turnSeq = useRef(0)
  const scroller = useRef<HTMLDivElement>(null)
  const bottom = useRef<HTMLDivElement>(null)
  // The drawer closes on a click outside itself, and the button that opens it is
  // not "outside" — otherwise pressing it while open closes and reopens the
  // drawer in one gesture.
  const historyButton = useRef<HTMLButtonElement>(null)

  /**
   * Leave the turn on the wire: stop it, stop it being billed, and disown it.
   *
   * Every "leave this conversation" path used to dispatch `reset` alone. That
   * cleared `streaming`, which re-enabled the composer while the stream was
   * still running — and when the abandoned turn finally ended, its `finally`
   * nulled the *new* turn's controller (killing Stop) and dispatched `settle`,
   * wiping a question, its steps and its streamed text off the screen mid-turn.
   * Bumping the token is what makes those late dispatches no-ops.
   */
  const abandonTurn = useCallback((cancelSessionId: number | null) => {
    turnSeq.current += 1
    const controller = abort.current
    abort.current = null
    if (!controller) {
      return
    }
    // Both halves, as everywhere else: the abort stops the browser reading and
    // the endpoint stops the server finishing — and billing — an Opus turn.
    controller.abort()
    if (cancelSessionId !== null) {
      void cancelTurn(cancelSessionId).catch(() => undefined)
    }
  }, [])

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
    // The app-wide default is `retry: 1`, which asked a known-dead id for a
    // second time and delayed the redirect below by the ~1 s backoff — long
    // enough to show the empty new-chat view under the dead URL. A 404 is an
    // answer, not a blip; everything else still gets its one retry.
    retry: (failureCount, error) => !isNotFound(error) && failureCount < 1,
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
  const liveSession = live.sessionId
  useEffect(() => {
    if (staleSession) {
      abandonTurn(liveSession)
      dispatch({ kind: 'reset' })
    }
  }, [abandonTurn, liveSession, staleSession])

  // ...and an id the router accepted but nothing could ever have owned goes back
  // to `/chat` on its own, since a disabled query never errors and the page would
  // otherwise render the empty new-chat view under a URL that says `/chat/abc`.
  // Routed only: the embedded pane does not read the URL, and the id it does read
  // is a number already. There is no live state to drop — a route that never
  // named a session cannot have started a turn on one.
  const badRouteId = !embedded && params.id !== undefined && routeSessionId === null
  useEffect(() => {
    if (badRouteId) {
      navigate('/chat', { replace: true })
    }
  }, [badRouteId, navigate])

  // A session that is not there — a hand-typed id, a stale bookmark, a tab left
  // open while the chat was deleted from another one. The empty view under
  // `/chat/999` looks like a working new chat until you send into it and the
  // POST 404s too, so the page goes back to `/chat` (replace: the dead id does
  // not deserve a history entry) and drops the live state with it. Only a 404:
  // every other failure keeps the error on screen, because a backend that is
  // down is not a session that is gone.
  //
  // The sessions list is refreshed too. The likeliest source of a 404 is the chat
  // having been deleted from another tab, and the drawer's cached list — 30 s of
  // `staleTime` — still holds the row: without this, clicking it bounces the user
  // back to `/chat` with no explanation, and clicking it again does the same.
  const missingSession = isNotFound(detail.error)
  useEffect(() => {
    if (!missingSession) {
      return
    }
    void queryClient.invalidateQueries({ queryKey: sessionsQueryKey })
    abandonTurn(liveSession)
    dispatch({ kind: 'reset' })
    // Routed: navigate. Embedded: the URL belongs to the other pane, so this
    // clears the local selection instead — `openSession` owns that fork, and in
    // that mode it is a `setState`. The server answering 404 is the external
    // system this effect exists to synchronise with, and there is nothing to
    // derive during render: the id being cleared is what the query that failed
    // was keyed on, so clearing it is what stops the effect running again.
    // oxlint-disable-next-line react/set-state-in-effect
    openSession(null, true)
  }, [abandonTurn, liveSession, missingSession, openSession, queryClient])

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
  const stored = useMemo(() => groupTurns(messages ?? [], feedTitles), [messages, feedTitles])
  // The backend persists the user row before the first token, so the refetch
  // that follows `createSession` already holds the question the live turn is
  // asking. Without this it rendered twice: once as this turn's heading and
  // again as the live turn's follow-up, with the steps under the second copy.
  const turns = useMemo(() => turnsBesideLive(stored, live.prompt), [stored, live.prompt])

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
        // user onto the blank /chat view — and its stream would keep running
        // against a session that no longer exists.
        abandonTurn(live.sessionId)
        dispatch({ kind: 'reset' })
        openSession(null)
      }
    },
  })

  const send = useCallback(
    async (text: string) => {
      // A send supersedes whatever is on the wire rather than racing it.
      abandonTurn(live.sessionId)
      const token = turnSeq.current
      // Every dispatch below belongs to *this* send. Creating a session is an
      // await, so the user can leave before the stream even opens.
      const mine = () => turnSeq.current === token

      let id = sessionId
      if (id === null) {
        const created = await createSession({})
        id = created.id
        await queryClient.invalidateQueries({ queryKey: sessionsQueryKey })
        if (!mine()) {
          return
        }
        openSession(id, true)
      }

      const itemIds = attached.map((item) => item.id)
      // The chips ride along on the live turn: they are stored on the user row,
      // and `turnsBesideLive` hides that row until the turn settles.
      const chips = attached.map((item) => ({ id: item.id, title: item.title, url: item.url }))
      setAttached([])
      dispatch({
        kind: 'start',
        prompt: text,
        sessionId: id,
        startedAt: Date.now(),
        attachments: chips,
        token,
      })

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
            dispatch({ kind: 'sse', event, payload, token })
          },
        })
      } catch (error) {
        if (controller.signal.aborted) {
          dispatch({
            kind: 'failed',
            error: { type: 'cancelled', message: 'Stopped.', category: null },
            token,
          })
        } else {
          const payload: ErrorPayload = {
            type: error instanceof SSEHttpError && error.status === 409 ? 'api_error' : 'connection',
            message: error instanceof Error ? error.message : String(error),
            category: null,
          }
          dispatch({ kind: 'failed', error: payload, token })
        }
      } finally {
        if (mine()) {
          abort.current = null
        }
        // The Query cache becomes the source of truth again once the turn ends.
        // Worth doing even for an abandoned turn: it still wrote to the session.
        await queryClient.invalidateQueries({ queryKey: sessionQueryKey(id) })
        await queryClient.invalidateQueries({ queryKey: sessionsQueryKey })
        // `settle`, not `reset`: a refusal, a stop or a connection failure has to
        // stay on screen until the next send, or the user is left looking at
        // their own question with nothing under it. Re-checked after the awaits,
        // which are long enough for the user to have left.
        if (mine()) {
          dispatch({ kind: 'settle', token })
        }
      }
    },
    [abandonTurn, attached, live.sessionId, openSession, queryClient, sessionId],
  )

  const stop = useCallback(() => {
    // Both halves are needed: the abort stops the browser reading, and the
    // endpoint stops the server billing. An SSE disconnect alone does neither.
    abort.current?.abort()
    // The turn's own session, not the route's. A browser-back to `/chat`
    // mid-turn leaves the turn on screen with its Stop button and no id in the
    // URL, and this branch is what made that state reachable — cancelling
    // `sessionId` there cancelled nothing and the server billed the whole turn.
    const cancelId = live.sessionId ?? sessionId
    if (cancelId !== null) {
      void cancelTurn(cancelId).catch(() => undefined)
    }
  }, [live.sessionId, sessionId])

  // Stable, because the drawer's Escape listener is subscribed to `document` for
  // as long as this prop is unchanged: an inline arrow re-subscribed it on every
  // render, and during a streaming turn this page renders many times a second.
  const closeHistory = useCallback(() => setHistoryOpen(false), [])

  const newChat = useCallback(() => {
    abandonTurn(live.sessionId)
    dispatch({ kind: 'reset' })
    setAttached([])
    setHistoryOpen(false)
    openSession(null)
  }, [abandonTurn, live.sessionId, openSession])

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
          ref={historyButton}
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
            abandonTurn(live.sessionId)
            dispatch({ kind: 'reset' })
            setHistoryOpen(false)
            openSession(id)
          }}
          onRename={(id, title) => rename.mutate({ id, title })}
          onArchive={(id, archived) => archive.mutate({ id, archived })}
          onDelete={(id) => remove.mutateAsync(id).then(() => undefined)}
          onLoadMore={() => void sessions.fetchNextPage()}
          onClose={closeHistory}
          openerRef={historyButton}
        />
      ) : null}

      {empty ? (
        <EmptyResearch
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
                  attachments={live.attachments}
                  steps={steps}
                  answer={live.text}
                  error={live.error}
                  followUp={turns.length > 0}
                  streaming={live.streaming}
                  progress={{
                    activity: live.activity,
                    activeTool: live.activeTool,
                    startedAt: live.startedAt,
                  }}
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
