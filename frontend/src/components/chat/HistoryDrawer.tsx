import { useEffect, useEffectEvent, useRef, useState, type RefObject } from 'react'

import { whenLabel, type ResearchSession } from '../../api/chat'
import { ApiError } from '../../api/client'
import Button from '../ui/Button'
import Checkbox from '../ui/Checkbox'
import ConfirmDialog from '../ui/ConfirmDialog'
import Icon from '../ui/Icon'
import IconButton from '../ui/IconButton'
import Input from '../ui/Input'
import { cx } from '../ui/classes'
import { isOutside } from '../ui/modal'

interface HistoryDrawerProps {
  sessions: ResearchSession[]
  activeId: number | null
  search: string
  showArchived: boolean
  isPending: boolean
  isError: boolean
  hasMore: boolean
  loadingMore: boolean
  /** The row with a rename, archive or delete in flight. */
  busyId: number | null
  /** Sessions with a turn in flight, server-side — not necessarily this page's. */
  running: Set<number>
  onSearchChange: (value: string) => void
  onShowArchivedChange: (show: boolean) => void
  onOpen: (id: number) => void
  onRename: (id: number, title: string) => void
  onArchive: (id: number, archived: boolean) => void
  /** Rejects if the delete failed — the confirm dialog stays open and says so. */
  onDelete: (id: number) => Promise<void>
  onLoadMore: () => void
  onClose: () => void
  /**
   * The button that opened the drawer, so a click on it is not "outside".
   * Without it the opener's `pointerdown` closes the drawer and its `click`
   * reopens it, and the toggle never appears to do anything.
   */
  openerRef?: RefObject<HTMLButtonElement | null>
}

/**
 * The list of chats, and everything you can do to one.
 *
 * The filter goes to the server rather than filtering the loaded page, because
 * the page is thirty rows of a history that grows without bound — and because
 * the server matches on what was *said* in a chat, which the rows do not carry.
 */
export default function HistoryDrawer({
  sessions,
  activeId,
  search,
  showArchived,
  isPending,
  isError,
  hasMore,
  loadingMore,
  busyId,
  running,
  onSearchChange,
  onShowArchivedChange,
  onOpen,
  onRename,
  onArchive,
  onDelete,
  onLoadMore,
  onClose,
  openerRef,
}: HistoryDrawerProps) {
  const panelRef = useRef<HTMLDivElement>(null)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [menuId, setMenuId] = useState<number | null>(null)
  const [draft, setDraft] = useState('')
  const [pendingDelete, setPendingDelete] = useState<ResearchSession | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  // Escape unwinds one layer at a time: the confirm dialog, the row menu, then a
  // rename in progress, then the drawer. Closing everything at once loses the
  // edit — or answers a question the user was still reading.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') {
        return
      }
      if (pendingDelete !== null) {
        // The dialog is modal and closes itself; this listener stays out of it.
        return
      }
      if (menuId !== null) {
        setMenuId(null)
      } else if (editingId !== null) {
        setEditingId(null)
      } else {
        onClose()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [editingId, menuId, onClose, pendingDelete])

  const commit = (id: number) => {
    const title = draft.trim()
    setEditingId(null)
    if (title) {
      onRename(id, title)
    }
  }

  /**
   * What a click outside the drawer does: **save a rename in flight, then close**.
   *
   * Clicking away used to save, because the click blurred the title input and
   * `onBlur` committed it. Closing on `pointerdown` took that away — the drawer
   * unmounts before the browser moves focus, and an element removed from the DOM
   * fires no `blur`, so the typed title vanished with no feedback. Saving is the
   * behaviour to keep: the user typed it, and Escape is right there to discard.
   * (Escape stays a one-layer-at-a-time unwind; clicking away closes outright,
   * which is what the gesture asks for.)
   *
   * `useEffectEvent`, so the listener below sees the current `editingId` and
   * `draft` without re-subscribing to `document` on every keystroke and every
   * parent render.
   */
  const closeFromOutside = useEffectEvent(() => {
    if (editingId !== null) {
      commit(editingId)
    }
    onClose()
  })

  // A click anywhere else closes the drawer — the panel covers the left edge of
  // the answer column, and reaching for the text under it had to go via the
  // header button.
  //
  // `pointerdown`, in the capture phase, so the decision is made on the way down
  // and before anything inside re-renders the node the event landed on: a
  // `click` handler that removed its own row would leave a target no longer in
  // the document, which `contains` reads as outside. Everything the drawer owns
  // — the row menu, its dismissal overlay and the delete dialog — is a DOM child
  // of the panel (nothing here renders into a portal), so one `contains` check
  // covers all of it.
  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      if (isOutside(event.target as Node | null, panelRef.current, openerRef?.current ?? null)) {
        closeFromOutside()
      }
    }
    document.addEventListener('pointerdown', onPointerDown, true)
    return () => document.removeEventListener('pointerdown', onPointerDown, true)
  }, [openerRef])

  const startEditing = (session: ResearchSession) => {
    setDraft(session.title ?? '')
    setEditingId(session.id)
    setMenuId(null)
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
      await onDelete(session.id)
      setPendingDelete(null)
    } catch (cause) {
      // The server's own reason, when there is one: "is the backend running?" is
      // misleading for a 409 or a 500, and it is the only thing the dialog says.
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
    <div
      ref={panelRef}
      className="absolute top-[49px] bottom-0 left-0 z-20 flex w-[262px] flex-col border-r border-line bg-panel shadow-[8px_0_24px_rgba(0,0,0,0.06)]"
    >
      <div className="border-b border-line p-2.5">
        <Input
          type="search"
          value={search}
          autoFocus
          placeholder="Filter chats…"
          aria-label="Filter chats"
          onChange={(event) => onSearchChange(event.target.value)}
          tone="bg"
        />
      </div>

      <div className="flex-1 overflow-y-auto p-1.5">
        <Body
          isPending={isPending}
          isError={isError}
          isEmpty={sessions.length === 0}
          searching={search.trim() !== ''}
          showArchived={showArchived}
        />

        {sessions.map((session) => {
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
                  tone="bg"
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
                onClick={() => onOpen(session.id)}
                onDoubleClick={() => startEditing(session)}
                title={session.title ?? 'Untitled chat'}
                className="block w-full cursor-pointer px-[9px] py-[7px] pr-7 text-left"
              >
                <span
                  className={cx(
                    'block truncate text-[12.5px]',
                    session.archived ? 'text-muted italic' : 'text-ink',
                  )}
                >
                  {session.title || 'Untitled chat'}
                </span>
                <RowMeta busy={busy} running={running.has(session.id)} session={session} />
              </button>

              <IconButton
                icon="ellipsis"
                label={`Actions for ${session.title || 'this chat'}`}
                size={13}
                disabled={busy}
                onClick={() => setMenuId((current) => (current === session.id ? null : session.id))}
                className={cx(
                  'absolute top-1.5 right-1 p-1 opacity-0 transition-opacity duration-150',
                  'group-hover:opacity-100 focus-visible:opacity-100',
                  menuId === session.id && 'opacity-100',
                )}
              />

              {menuId === session.id ? (
                <>
                  {/* Catches the click that should dismiss the menu. */}
                  <div className="fixed inset-0 z-30" onClick={() => setMenuId(null)} />
                  <div className="absolute top-7 right-1 z-40 w-[138px] overflow-hidden rounded-[8px] border border-line bg-panel py-1 shadow-[0_8px_24px_rgba(0,0,0,0.16)]">
                    <MenuItem icon="edit" onClick={() => startEditing(session)}>
                      Rename
                    </MenuItem>
                    <MenuItem
                      icon={session.archived ? 'unarchive' : 'archive'}
                      onClick={() => {
                        setMenuId(null)
                        onArchive(session.id, !session.archived)
                      }}
                    >
                      {session.archived ? 'Unarchive' : 'Archive'}
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

        {hasMore ? (
          <div className="px-0.5 pt-2">
            <Button size="sm" loading={loadingMore} onClick={onLoadMore} className="w-full">
              Load more
            </Button>
          </div>
        ) : null}
      </div>

      <div className="border-t border-line px-3 py-2.5">
        <Checkbox
          checked={showArchived}
          onChange={onShowArchivedChange}
          label={<span className="text-[11px] font-normal text-muted">Show archived</span>}
        />
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
 * The row's second line: what it is doing, or when it was last touched.
 *
 * A turn keeps running after the user opens another chat, so the drawer is
 * where they find it again — and "running" is more use there than a timestamp
 * that has stopped meaning anything.
 */
function RowMeta({
  busy,
  running,
  session,
}: {
  busy: boolean
  running: boolean
  session: ResearchSession
}) {
  if (busy) {
    return <span className="mt-px block text-[10.5px] text-faint">Saving…</span>
  }
  if (running) {
    return (
      <span className="mt-px flex items-center gap-1 text-[10.5px] text-accent">
        <Icon name="spinner" size={11} />
        running
      </span>
    )
  }
  return (
    <span className="mt-px block text-[10.5px] text-faint">{whenLabel(session.updated_at)}</span>
  )
}

function MenuItem({
  icon,
  danger = false,
  onClick,
  children,
}: {
  icon: 'edit' | 'archive' | 'unarchive' | 'trash'
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

interface BodyProps {
  isPending: boolean
  isError: boolean
  isEmpty: boolean
  searching: boolean
  showArchived: boolean
}

/** The states the list can be in before it has rows to show. */
function Body({ isPending, isError, isEmpty, searching, showArchived }: BodyProps) {
  if (isPending) {
    return <p className="px-2 py-6 text-center text-[11.5px] text-faint">Loading chats…</p>
  }
  if (isError) {
    return (
      <p className="px-2 py-6 text-center text-[11.5px] text-red">
        Could not load chats. Is the backend running?
      </p>
    )
  }
  if (!isEmpty) {
    return null
  }
  if (searching) {
    return <p className="px-2 py-6 text-center text-[11.5px] text-faint">No chats match.</p>
  }
  if (showArchived) {
    return <p className="px-2 py-6 text-center text-[11.5px] text-faint">No archived chats.</p>
  }
  return <p className="px-2 py-6 text-center text-[11.5px] text-faint">No chats yet.</p>
}
