import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'

import {
  cancelTurn,
  createSession,
  deleteSession,
  fetchSession,
  fetchSessions,
  messagesUrl,
  renameSession,
  sessionQueryKey,
  sessionsListKey,
  sessionsQueryKey,
  setSessionArchived,
  type ErrorPayload,
  type SessionFilters,
} from '../api/chat'
import type { FeedItem } from '../api/inbox'
import Composer from '../components/chat/Composer'
import { emptyTurn, liveTurnReducer } from '../components/chat/liveTurn'
import Markdown from '../components/chat/Markdown'
import SessionSidebar from '../components/chat/SessionSidebar'
import ThinkingPane from '../components/chat/ThinkingPane'
import ToolCallCard from '../components/chat/ToolCallCard'
import Transcript from '../components/chat/Transcript'
import TurnError from '../components/chat/TurnError'
import useDebouncedValue from '../components/inbox/useDebouncedValue'
import GenerateNotesDialog from '../components/notes/GenerateNotesDialog'
import { SSEHttpError, streamSSE } from '../lib/sse'

/** What the Inbox's "Research these" button hands over. */
export interface ChatNavigationState {
  attachedItems?: FeedItem[]
}

export default function ChatPage() {
  const params = useParams<{ id?: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const queryClient = useQueryClient()

  const sessionId = params.id ? Number(params.id) : null
  const [live, dispatch] = useReducer(liveTurnReducer, emptyTurn)
  // Items handed over by the Inbox's "Research these" ride in on route state, so
  // they are the initial value rather than something an effect sets afterwards.
  const [attached, setAttached] = useState<FeedItem[]>(
    () => (location.state as ChatNavigationState | null)?.attachedItems ?? [],
  )
  const [notesOpen, setNotesOpen] = useState(false)
  const [sessionSearch, setSessionSearch] = useState('')
  const [showArchived, setShowArchived] = useState(false)
  const abort = useRef<AbortController | null>(null)
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
  })

  const detail = useQuery({
    queryKey: sessionQueryKey(sessionId ?? 0),
    queryFn: () => fetchSession(sessionId as number),
    enabled: sessionId !== null,
  })

  // Leaving the conversation the live state belongs to drops it — this is what
  // catches browser back/forward, which no click handler sees. It cannot fire
  // mid-turn: `send` stamps the new session id onto the state before navigating,
  // so the two only diverge once the user has genuinely moved on.
  const staleSession = live.sessionId !== null && live.sessionId !== sessionId
  useEffect(() => {
    if (staleSession) {
      dispatch({ kind: 'reset' })
    }
  }, [staleSession])

  // Clear the handover off the history entry so a reload does not re-attach.
  useEffect(() => {
    if ((location.state as ChatNavigationState | null)?.attachedItems) {
      navigate(location.pathname, { replace: true, state: null })
    }
  }, [location.pathname, location.state, navigate])

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth' })
  }, [live.text, live.cards.length, detail.data?.messages.length])

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
        navigate('/chat')
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
        navigate(`/chat/${id}`, { replace: true })
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
    [attached, navigate, queryClient, sessionId],
  )

  const stop = useCallback(() => {
    // Both halves are needed: the abort stops the browser reading, and the
    // endpoint stops the server billing. An SSE disconnect alone does neither.
    abort.current?.abort()
    if (sessionId !== null) {
      void cancelTurn(sessionId).catch(() => undefined)
    }
  }, [sessionId])

  const allSessions = sessions.data?.pages.flatMap((page) => page.sessions) ?? []
  // Which row is mid-write, so the sidebar can grey it out while it saves.
  const busyId =
    (rename.isPending ? rename.variables?.id : undefined) ??
    (archive.isPending ? archive.variables?.id : undefined) ??
    (remove.isPending ? remove.variables : undefined) ??
    null
  const messages = detail.data?.messages ?? []
  const awaitingFirstText = live.streaming && live.text === ''

  return (
    <div className="flex h-full">
      <SessionSidebar
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
        onNew={() => {
          dispatch({ kind: 'reset' })
          navigate('/chat')
        }}
        onOpen={(id) => {
          dispatch({ kind: 'reset' })
          navigate(`/chat/${id}`)
        }}
        onRename={(id, title) => rename.mutate({ id, title })}
        onArchive={(id, archived) => archive.mutate({ id, archived })}
        onDelete={(id) => remove.mutate(id)}
        onLoadMore={() => void sessions.fetchNextPage()}
      />

      <section className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-start justify-between gap-3 border-b border-slate-200 px-6 py-3">
          <div className="min-w-0">
            <h1 className="truncate text-sm font-semibold text-slate-900">
              {detail.data?.session.title || 'New chat'}
            </h1>
            {detail.data ? (
              <p className="text-xs text-slate-500">
                {detail.data.session.model} · {detail.data.session.total_input_tokens} in /{' '}
                {detail.data.session.total_output_tokens} out
              </p>
            ) : (
              <p className="text-xs text-slate-500">
                Ask about the inbox, an advisory, or a URL you paste.
              </p>
            )}
          </div>
          {sessionId !== null ? (
            <button
              type="button"
              disabled={live.streaming}
              onClick={() => setNotesOpen(true)}
              className="shrink-0 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:border-slate-400 disabled:opacity-40"
            >
              Generate notes from this session
            </button>
          ) : null}
        </header>

        <div className="flex-1 overflow-y-auto px-6 py-4">
          {sessionId === null && messages.length === 0 && !live.prompt ? (
            <p className="mx-auto max-w-xl pt-16 text-center text-sm text-slate-500">
              Start a chat. The assistant searches your local feed inbox first, then the web.
            </p>
          ) : null}

          <Transcript messages={messages} />

          {live.prompt !== null || live.error !== null ? (
            <div className="mt-4 space-y-2">
              {live.prompt !== null ? (
                <div className="flex justify-end">
                  <div className="max-w-2xl whitespace-pre-wrap rounded-lg bg-slate-900 px-3 py-2 text-sm text-white">
                    {live.prompt}
                  </div>
                </div>
              ) : null}

              <ThinkingPane text={live.thinking} streaming={awaitingFirstText} />
              {live.cards.map((card) => (
                <ToolCallCard key={card.toolUseId} card={card} />
              ))}
              {live.text ? <Markdown>{live.text}</Markdown> : null}
              {live.error ? <TurnError error={live.error} /> : null}
            </div>
          ) : null}

          <div ref={bottom} />
        </div>

        {notesOpen && sessionId !== null ? (
          <GenerateNotesDialog
            initialSessionId={sessionId}
            onClose={() => setNotesOpen(false)}
          />
        ) : null}

        <Composer
          streaming={live.streaming}
          attached={attached}
          onAttach={(item) =>
            setAttached((current) =>
              current.some((existing) => existing.id === item.id) ? current : [...current, item],
            )
          }
          onDetach={(id) => setAttached((current) => current.filter((item) => item.id !== id))}
          onSend={(text) => void send(text)}
          onStop={stop}
        />
      </section>
    </div>
  )
}
