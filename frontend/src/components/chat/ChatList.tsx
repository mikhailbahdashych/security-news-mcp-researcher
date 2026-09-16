import { useInfiniteQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import {
  fetchSessions,
  groupSessionsByDay,
  sessionsListKey,
  type ResearchSession,
  type SessionFilters,
} from '../../api/chat'
import { ApiError } from '../../api/client'
import useDebouncedValue from '../../lib/useDebouncedValue'
import { useRunningTurns } from '../../lib/useRunningTurns'
import ConfirmDialog from '../ui/ConfirmDialog'
import Icon from '../ui/Icon'
import IconButton from '../ui/IconButton'
import Input from '../ui/Input'
import { SECTION_LABEL, cx } from '../ui/classes'
import { useSessionActions } from './useSessionActions'

export interface ChatListProps {
  /** The chat the routed pane has open, so its row reads as current. */
  activeId: number | null
}

/**
 * The chat history, in the rail.
 *
 * It used to be an overlay over the research view, which meant the list was
 * either in the way or out of sight. The rail already has the room while it is
 * expanded, and the history is the one list worth keeping beside the page it
 * belongs to.
 *
 * It owns its own queries: the rail is app-level and the research page may not
 * even be the pane the user is looking at, so there is nothing above it to hand
 * the rows down. `activeId` is the whole of its input.
 *
 * The filter goes to the server rather than filtering the loaded page, because
 * the page is thirty rows of a history that grows without bound — and because
 * the server matches on what was *said* in a chat, which the rows do not carry.
 */
export default function ChatList({ activeId }: ChatListProps) {
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  const [editingId, setEditingId] = useState<number | null>(null)
  const [menuId, setMenuId] = useState<number | null>(null)
  const [draft, setDraft] = useState('')
  const [pendingDelete, setPendingDelete] = useState<ResearchSession | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const scroller = useRef<HTMLDivElement>(null)
  const sentinel = useRef<HTMLDivElement>(null)

  const debouncedSearch = useDebouncedValue(search)
  // Never the archived ones. They moved to Settings' own dialog, because a
  // checkbox that quietly swapped the contents of this list was a mode with no
  // way of telling, from a row, which list you were in.
  const filters = useMemo<SessionFilters>(
    () => ({ q: debouncedSearch, archived: 'false' }),
    [debouncedSearch],
  )

  const sessions = useInfiniteQuery({
    queryKey: sessionsListKey(filters),
    queryFn: ({ pageParam }) => fetchSessions(filters, pageParam as string | undefined),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  })

  const running = useRunningTurns()
  const { rename, archive, remove, busyId } = useSessionActions()

  // Grouped off the query's own data, not off a list rebuilt per render: this
  // component re-renders on every keystroke in the filter.
  const days = useMemo(
    () => groupSessionsByDay(sessions.data?.pages.flatMap((page) => page.sessions) ?? []),
    [sessions.data],
  )

  const { hasNextPage, isFetchingNextPage, fetchNextPage } = sessions

  // The next page loads when the bottom of the list comes into view, rather than
  // on a button: the rail is a sidebar, and a "Load more" at the end of a column
  // of chats is a row you have to aim at to keep reading.
  //
  // Re-armed whenever a page settles. An observer delivers its first record as
  // soon as it observes, so if the sentinel is *still* on screen — a short page
  // in a tall rail — the next page follows immediately, and the loop stops the
  // moment the sentinel is pushed below the fold or the pages run out.
  useEffect(() => {
    const node = sentinel.current
    if (!node || !hasNextPage || isFetchingNextPage) {
      return
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          void fetchNextPage()
        }
      },
      { root: scroller.current, rootMargin: '120px' },
    )
    observer.observe(node)
    return () => observer.disconnect()
  }, [hasNextPage, isFetchingNextPage, fetchNextPage])

  // Escape unwinds one layer at a time — the row menu, then a rename in
  // progress. There is no third layer: the list is part of the rail and has
  // nothing to close. The delete dialog is modal and closes itself.
  useEffect(() => {
    if (pendingDelete !== null || (menuId === null && editingId === null)) {
      return
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') {
        return
      }
      if (menuId !== null) {
        setMenuId(null)
      } else {
        setEditingId(null)
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [editingId, menuId, pendingDelete])

  const startEditing = (session: ResearchSession) => {
    setDraft(session.title ?? '')
    setEditingId(session.id)
    setMenuId(null)
  }

  /** Save on Enter and on the way out; Escape unmounts the input and discards. */
  const commit = (id: number) => {
    const title = draft.trim()
    setEditingId(null)
    if (title) {
      rename(id, title)
    }
  }

  /**
   * Delete, then close — not the other way round.
   *
   * Closing first meant the dialog's `busy` and `error` props could never be
   * true: a delete that failed closed the dialog, left the row in the list and
   * said nothing at all, so the user's next move was to press Delete again.
   */
  const confirmDelete = async (session: ResearchSession) => {
    setDeleting(true)
    setDeleteError(null)
    try {
      await remove(session.id)
      setPendingDelete(null)
    } catch (cause) {
      setDeleteError(
        cause instanceof ApiError
          ? cause.detail
          : 'Could not delete this chat. Is the backend running?',
      )
    } finally {
      setDeleting(false)
    }
  }

  return (
    <div className="mt-1.5 flex min-h-0 flex-1 flex-col">
      <div className="mx-1 mb-2 h-px bg-line" />

      <Input
        type="search"
        value={search}
        placeholder="Filter chats…"
        aria-label="Filter chats"
        onChange={(event) => setSearch(event.target.value)}
        className="px-2.5 py-1 text-[11.5px]"
      />

      <div ref={scroller} className="-mx-1 mt-1.5 min-h-0 flex-1 overflow-y-auto px-1">
        <Body
          isPending={sessions.isPending}
          isError={sessions.isError}
          isEmpty={days.length === 0}
          searching={search.trim() !== ''}
        />

        {days.map((day) => (
          <div key={day.label}>
            <p className={cx(SECTION_LABEL, 'px-1.5 pt-2.5 pb-1')}>{day.label}</p>

            {day.sessions.map((session) => {
              const busy = busyId === session.id
              if (editingId === session.id) {
                return (
                  <div key={session.id} className="px-0.5 py-0.5">
                    <Input
                      autoFocus
                      value={draft}
                      aria-label="Chat title"
                      onChange={(event) => setDraft(event.target.value)}
                      onBlur={() => commit(session.id)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter') {
                          commit(session.id)
                        }
                      }}
                      className="px-2 py-1 text-[11.5px]"
                    />
                  </div>
                )
              }

              return (
                <div
                  key={session.id}
                  className={cx(
                    'group relative rounded-[8px] transition-colors duration-150',
                    session.id === activeId ? 'bg-accent-soft' : 'hover:bg-hover',
                    busy && 'opacity-50',
                  )}
                >
                  <button
                    type="button"
                    // The rail is app-level, so opening a chat is a navigation
                    // and nothing else: the routed pane is the one the URL
                    // steers, and the research page resets its own live turn
                    // when the route names a different session.
                    onClick={() => navigate(`/chat/${session.id}`)}
                    onDoubleClick={() => startEditing(session)}
                    title={session.title ?? 'Untitled chat'}
                    aria-current={session.id === activeId ? 'page' : undefined}
                    className="block w-full cursor-pointer px-[7px] py-[5px] pr-6 text-left"
                  >
                    <span
                      className={cx(
                        'block truncate text-[12px]',
                        session.id === activeId ? 'text-accent' : 'text-muted',
                      )}
                    >
                      {session.title || 'Untitled chat'}
                    </span>
                    <RowMeta busy={busy} running={running.has(session.id)} />
                  </button>

                  <IconButton
                    icon="ellipsis"
                    label={`Actions for ${session.title || 'this chat'}`}
                    size={13}
                    disabled={busy}
                    onClick={() =>
                      setMenuId((current) => (current === session.id ? null : session.id))
                    }
                    className={cx(
                      'absolute top-1 right-0.5 p-1 opacity-0 transition-opacity duration-150',
                      'group-hover:opacity-100 focus-visible:opacity-100',
                      menuId === session.id && 'opacity-100',
                    )}
                  />

                  {menuId === session.id ? (
                    <>
                      {/* Catches the click that should dismiss the menu. */}
                      <div className="fixed inset-0 z-30" onClick={() => setMenuId(null)} />
                      <div className="absolute top-6 right-0.5 z-40 w-[138px] overflow-hidden rounded-[8px] border border-line bg-panel py-1 shadow-[0_8px_24px_rgba(0,0,0,0.16)]">
                        <MenuItem icon="edit" onClick={() => startEditing(session)}>
                          Rename
                        </MenuItem>
                        <MenuItem
                          icon="archive"
                          onClick={() => {
                            setMenuId(null)
                            archive(session.id, true)
                          }}
                        >
                          Archive
                        </MenuItem>
                        <MenuItem
                          icon="trash"
                          danger
                          onClick={() => {
                            setMenuId(null)
                            setPendingDelete(session)
                          }}
                        >
                          Delete
                        </MenuItem>
                      </div>
                    </>
                  ) : null}
                </div>
              )
            })}
          </div>
        ))}

        <div ref={sentinel} aria-hidden="true" className="h-px" />
        {isFetchingNextPage ? (
          <p className="px-2 py-2 text-center text-[10.5px] text-faint">Loading…</p>
        ) : null}
      </div>

      {pendingDelete ? (
        <ConfirmDialog
          title="Delete chat"
          // Spelled out because the two halves have different fates, and a user
          // who thinks the write-up goes too will never press the button.
          message={
            <>
              “{pendingDelete.title || 'This chat'}” and every message and tool call in it will be
              deleted permanently. Notes generated from it are kept — they just stop linking back
              here.
            </>
          }
          confirmLabel="Delete chat"
          busy={deleting}
          error={deleteError}
          onCancel={() => {
            if (deleting) {
              return
            }
            setPendingDelete(null)
            setDeleteError(null)
          }}
          onConfirm={() => void confirmDelete(pendingDelete)}
        />
      ) : null}
    </div>
  )
}

/**
 * The row's second line — but only when there is something to say.
 *
 * A turn keeps running after the user opens another chat, so this list is where
 * they find it again. The date is not repeated here: it is the group's header.
 */
function RowMeta({ busy, running }: { busy: boolean; running: boolean }) {
  if (busy) {
    return <span className="mt-px block text-[10px] text-faint">Saving…</span>
  }
  if (running) {
    return (
      <span className="mt-px flex items-center gap-1 text-[10px] text-accent">
        <Icon name="spinner" size={10} />
        running
      </span>
    )
  }
  return null
}

function MenuItem({
  icon,
  danger = false,
  onClick,
  children,
}: {
  icon: 'edit' | 'archive' | 'trash'
  danger?: boolean
  onClick: () => void
  children: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cx(
        'flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[12px] transition-colors duration-150 hover:bg-hover',
        danger ? 'text-muted hover:text-red' : 'text-ink',
      )}
    >
      <Icon name={icon} size={13} className="shrink-0 text-faint" />
      {children}
    </button>
  )
}

/** The states the list can be in before it has rows to show. */
function Body({
  isPending,
  isError,
  isEmpty,
  searching,
}: {
  isPending: boolean
  isError: boolean
  isEmpty: boolean
  searching: boolean
}) {
  if (isPending) {
    return <p className="px-2 py-6 text-center text-[11px] text-faint">Loading chats…</p>
  }
  if (isError) {
    return (
      <p className="px-2 py-6 text-center text-[11px] text-red">
        Could not load chats. Is the backend running?
      </p>
    )
  }
  if (!isEmpty) {
    return null
  }
  return (
    <p className="px-2 py-6 text-center text-[11px] text-faint">
      {searching ? 'No chats match.' : 'No chats yet.'}
    </p>
  )
}
