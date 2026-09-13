import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
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
  sessionsQueryKey,
  type ErrorPayload,
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
  const abort = useRef<AbortController | null>(null)
  const bottom = useRef<HTMLDivElement>(null)

  const sessions = useInfiniteQuery({
    queryKey: sessionsQueryKey,
    queryFn: ({ pageParam }) => fetchSessions(pageParam as string | undefined),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  })

  const detail = useQuery({
    queryKey: sessionQueryKey(sessionId ?? 0),
    queryFn: () => fetchSession(sessionId as number),
    enabled: sessionId !== null,
  })

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

  const remove = useMutation({
    mutationFn: (id: number) => deleteSession(id),
    onSuccess: (_data, id) => {
      queryClient.invalidateQueries({ queryKey: sessionsQueryKey })
      if (id === sessionId) {
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
      dispatch({ kind: 'start', prompt: text })

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
        dispatch({ kind: 'reset' })
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
  const messages = detail.data?.messages ?? []
  const awaitingFirstText = live.streaming && live.text === ''

  return (
    <div className="flex h-full">
      <SessionSidebar
        sessions={allSessions}
        activeId={sessionId}
        hasMore={Boolean(sessions.hasNextPage)}
        loadingMore={sessions.isFetchingNextPage}
        onNew={() => {
          dispatch({ kind: 'reset' })
          navigate('/chat')
        }}
        onOpen={(id) => {
          dispatch({ kind: 'reset' })
          navigate(`/chat/${id}`)
        }}
        onRename={(id, title) => rename.mutate({ id, title })}
        onDelete={(id) => remove.mutate(id)}
        onLoadMore={() => void sessions.fetchNextPage()}
      />

      <section className="flex min-w-0 flex-1 flex-col">
        <header className="border-b border-slate-200 px-6 py-3">
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
        </header>

        <div className="flex-1 overflow-y-auto px-6 py-4">
          {sessionId === null && messages.length === 0 && !live.prompt ? (
            <p className="mx-auto max-w-xl pt-16 text-center text-sm text-slate-500">
              Start a chat. The assistant searches your local feed inbox first, then the web.
            </p>
          ) : null}

          <Transcript messages={messages} />

          {live.prompt !== null ? (
            <div className="mt-4 space-y-2">
              <div className="flex justify-end">
                <div className="max-w-2xl whitespace-pre-wrap rounded-lg bg-slate-900 px-3 py-2 text-sm text-white">
                  {live.prompt}
                </div>
              </div>

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
