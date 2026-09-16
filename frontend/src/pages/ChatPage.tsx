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
  renameSession,
  resendPayload,
  runningSessionsKey,
  sessionQueryKey,
  sessionsListKey,
  sessionsQueryKey,
  setSessionArchived,
  startTurn,
  streamUrl,
  type ErrorPayload,
  type ResendPayload,
  type SessionFilters,
} from '../api/chat'
import { ApiError } from '../api/client'
import { useFeedTitlesFromCache, type FeedItem } from '../api/inbox'
import { fetchSettings, settingsQueryKey } from '../api/settings'
import AnswerTurn from '../components/chat/AnswerTurn'
import Composer from '../components/chat/Composer'
import EmptyResearch from '../components/chat/EmptyResearch'
import HistoryDrawer from '../components/chat/HistoryDrawer'
import InterruptedNotice from '../components/chat/InterruptedNotice'
import TurnError from '../components/chat/TurnError'
import {
  emptyTurn,
  isForeignSession,
  liveSteps,
  liveTurnReducer,
  shouldAttach,
  turnsBesideLive,
} from '../components/chat/liveTurn'
import GenerateNotesDialog from '../components/notes/GenerateNotesDialog'
import Button from '../components/ui/Button'
import IconButton from '../components/ui/IconButton'
import type { EmbeddablePageProps } from '../components/ui/PageHost'
import { SSEHttpError, streamSSE } from '../lib/sse'
import useDebouncedValue from '../lib/useDebouncedValue'
import { useNow } from '../lib/useElapsed'
import { runningHeaderMeta, useRunningTurns } from '../lib/useRunningTurns'

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
  // Which send owns the live state. Bumped whenever a turn is abandoned, so a
  // reader that is still running cannot write to the turn that replaced it.
  const turnSeq = useRef(0)
  const scroller = useRef<HTMLDivElement>(null)
  const bottom = useRef<HTMLDivElement>(null)

  /**
   * Stop watching the turn and disown it — but let it run.
   *
   * The turn belongs to the session, not to this page: it keeps going with
   * nobody attached, and whoever opens the session next picks it up again. So
   * leaving detaches and nothing more; Stop and Delete are the two deliberate
   * cancels.
   *
   * Bumping the token is still the load-bearing half. Every "leave this
   * conversation" path used to dispatch `reset` alone, which cleared
   * `streaming` while the reader ran on — and when the abandoned reader finally
   * ended, its `finally` nulled the *new* turn's controller (killing Stop) and
   * dispatched `settle`, wiping a question, its steps and its streamed text off
   * the screen mid-turn. An older token makes those late dispatches no-ops.
   */
  const abandonTurn = useCallback(() => {
    turnSeq.current += 1
    abort.current?.abort()
    abort.current = null
  }, [])

  /**
   * Watch a turn: replay it from its first event, then follow it to `done`.
   *
   * The single owner of the stream, whether this page started the turn or found
   * it already running. Detaching (the abort) is not stopping: the turn goes on
   * server-side, so there is nothing to report and nothing to keep on screen.
   */
  const attach = useCallback(
    async (id: number, token: number) => {
      const mine = () => turnSeq.current === token
      if (!mine()) {
        // The user left between the decision to attach and this call. Opening a
        // reader now would only hand `abort.current` to a turn nobody is
        // watching, and take Stop away from the one that replaced it.
        return
      }
      const controller = new AbortController()
      abort.current = controller
      try {
        await streamSSE({
          url: streamUrl(id),
          method: 'GET',
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
          // Detached, not stopped: the turn goes on server-side. Nothing to show.
        } else if (error instanceof SSEHttpError && error.status === 204) {
          // Nothing running any more — the transcript has the answer.
        } else {
          const payload: ErrorPayload = {
            type: error instanceof SSEHttpError ? 'api_error' : 'connection',
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
        // Worth doing even for an abandoned reader: the turn still wrote to the
        // session, and the rail's dot is keyed on the running list.
        await queryClient.invalidateQueries({ queryKey: sessionQueryKey(id) })
        // `['sessions']` is a prefix, so this covers `runningSessionsKey`
        // (`['sessions', 'running']`) as well — and awaiting it is what makes
        // the running list right *before* `settle`, so the attach rule below
        // does not read a list that still names this session.
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
    [queryClient],
  )

  // Leaving Research for another page unmounts this one, and without this the
  // reader went on consuming the stream to the end of the turn: every return
  // opened another, and HTTP/1.1 gives a browser about six per origin. It is a
  // detach, not a cancel — the turn is the server's and keeps running, which is
  // what the rail's dot goes on saying.
  //
  // It also makes StrictMode's double-invoke harmless: the simulated unmount
  // runs this cleanup between the two passes, so the first pass's reader is
  // aborted and its token retired, and exactly one reader survives.
  useEffect(() => () => abandonTurn(), [abandonTurn])

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
    // Against the app-wide defaults (30 s stale, no focus refetch), because this
    // row is what says whether a turn is running. Reading it out of a 30 s cache
    // was why "send, walk to the Inbox, come back" showed an empty page with the
    // composer enabled, and why a second tab never noticed the turn at all.
    refetchOnMount: 'always',
    refetchOnWindowFocus: true,
  })

  // The configured model, for the composer's "what will answer this" line. The
  // session's own model is authoritative once there is one.
  const settings = useQuery({ queryKey: settingsQueryKey, queryFn: fetchSettings })

  // Which sessions are busy server-side, for the drawer's marks. The same query
  // the rail's dot reads, so the two cannot disagree about what is running.
  const running = useRunningTurns()

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
      abandonTurn()
      dispatch({ kind: 'reset' })
    }
  }, [abandonTurn, staleSession])

  // Joining a turn this page did not start: a reload, a return from the Inbox,
  // the drawer, a second tab. The rule itself is `shouldAttach`, a tested
  // function in `liveTurn.ts` — it weighs two caches against each other and is
  // exactly the kind of thing this project keeps out of components. Declared
  // after the effect above so that a browser-back onto another running session
  // resets the old turn first and this one then claims a token of its own.
  const turnStatus = detail.data?.session.turn_status ?? null
  const attachNow = shouldAttach({ sessionId, turnStatus, runningIds: running, live })
  useEffect(() => {
    if (!attachNow || sessionId === null) {
      return
    }
    turnSeq.current += 1
    const token = turnSeq.current
    dispatch({ kind: 'attach', sessionId, token })
    void attach(sessionId, token)
  }, [attachNow, sessionId, attach])

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
        abandonTurn()
        dispatch({ kind: 'reset' })
        openSession(null)
      }
    },
  })

  const send = useCallback(
    async (text: string, resent?: ResendPayload) => {
      // A send supersedes whatever this page was watching rather than racing it.
      abandonTurn()
      const token = turnSeq.current
      // Every dispatch below belongs to *this* send. Creating a session and
      // starting the turn are awaits, so the user can leave before either lands.
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

      // "Send again" carries the interrupted question's own items — both the ids
      // it sends and the chips it draws, read back off the stored user row. The
      // attachment picker belongs to the *next* question, so a resend neither
      // sends what is in it nor empties it; anything else is the picker.
      //
      // The chips ride along on the live turn: they are stored on the user row,
      // and `turnsBesideLive` hides that row until the turn settles.
      const itemIds = resent ? resent.attached_item_ids : attached.map((item) => item.id)
      const chips = resent
        ? resent.attachments
        : attached.map((item) => ({ id: item.id, title: item.title, url: item.url }))
      if (!resent) {
        setAttached([])
      }
      // Before the request, not after it: the question has to appear the moment
      // it is asked, and `start` is also what claims the live state for this
      // token — a `failed` dispatched before it would be ignored as stale. The
      // replayed `turn_started` corrects `startedAt` to the server's own.
      dispatch({
        kind: 'start',
        prompt: text,
        sessionId: id,
        startedAt: Date.now(),
        attachments: chips,
        token,
      })

      try {
        await startTurn(id, { content: text, attached_item_ids: itemIds })
      } catch (error) {
        const failure = error instanceof ApiError ? error : null
        let message = String(error)
        if (failure) {
          // The detail alone: `ApiError.message` prefixes it with `API 409:`,
          // which is a status line, not something to show someone.
          message = failure.detail
        } else if (error instanceof Error) {
          message = error.message
        }
        dispatch({
          kind: 'failed',
          error: { type: failure ? 'api_error' : 'connection', message, category: null },
          token,
        })
        if (failure?.status === 409) {
          // A turn *is* running on this session — this page simply did not start
          // it. Refresh both signals the attach rule reads, and it will pick the
          // running turn up instead of leaving an error card over an empty page.
          void queryClient.invalidateQueries({ queryKey: sessionQueryKey(id) })
          void queryClient.invalidateQueries({ queryKey: runningSessionsKey })
        }
        return
      }
      // The turn is the server's now, so the rail's dot is already wrong.
      void queryClient.invalidateQueries({ queryKey: runningSessionsKey })
      await attach(id, token)
    },
    [abandonTurn, attach, attached, openSession, queryClient, sessionId],
  )

  /**
   * Ask the interrupted question again, exactly as it was asked.
   *
   * Read back off the stored user row rather than kept in state: the restart
   * that cut the turn short took this page's memory of it with it, and the row
   * is the only record of which items were pinned.
   */
  const resend = useCallback(() => {
    const payload = resendPayload(messages ?? [])
    if (payload) {
      void send(payload.content, payload)
    }
  }, [messages, send])

  const stop = useCallback(() => {
    // Cancelling is the whole of Stop now. Disconnecting stops nothing — the
    // turn is the server's — and aborting the reader here would also throw away
    // the turn's own ending: the registry appends a `cancelled` error and a
    // `done` on its way out, which is what puts the "Stopped" notice on screen.
    //
    // The turn's own session, not the route's. A browser-back to `/chat`
    // mid-turn leaves the turn on screen with its Stop button and no id in the
    // URL, and cancelling `sessionId` there cancelled nothing while the server
    // billed the whole turn.
    const cancelId = live.sessionId ?? sessionId
    if (cancelId !== null) {
      void cancelTurn(cancelId).catch(() => undefined)
    }
  }, [live.sessionId, sessionId])

  const newChat = useCallback(() => {
    abandonTurn()
    dispatch({ kind: 'reset' })
    setAttached([])
    setHistoryOpen(false)
    openSession(null)
  }, [abandonTurn, openSession])

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
          {/* Either line shrinks four times faster than the title: in a narrow
              split pane the name of the chat is worth more than its meta. While
              a turn runs, how long it has been running is the only part of that
              meta still true — the token counts are the session's totals as of
              the last turn that finished. */}
          {turnStatus === 'running' ? (
            <RunningMeta startedAt={session?.turn_started_at ?? null} />
          ) : headerMeta ? (
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
          running={running}
          onSearchChange={setSessionSearch}
          onShowArchivedChange={setShowArchived}
          onOpen={(id) => {
            abandonTurn()
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

              {turnStatus === 'interrupted' && !live.streaming ? (
                <InterruptedNotice onResend={resend} />
              ) : null}

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

/**
 * `running · 1m 05s` in the header, counted from the server's own start time.
 *
 * A component of its own so the once-a-second tick lives exactly as long as the
 * counter does — in the page it would re-render the whole transcript every
 * second, including every second nothing is running.
 */
function RunningMeta({ startedAt }: { startedAt: string | null }) {
  const now = useNow(1_000)
  const meta = runningHeaderMeta(startedAt, now)
  if (!meta) {
    return null
  }
  return <span className="min-w-0 shrink-4 truncate text-[11px] text-accent">{meta}</span>
}
