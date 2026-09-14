import { useState } from 'react'

import type { ResearchSession } from '../../api/chat'

interface SessionSidebarProps {
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
  onSearchChange: (value: string) => void
  onShowArchivedChange: (show: boolean) => void
  onNew: () => void
  onOpen: (id: number) => void
  onRename: (id: number, title: string) => void
  onArchive: (id: number, archived: boolean) => void
  onDelete: (id: number) => void
  onLoadMore: () => void
}

const iconButtonClass = 'px-1 text-xs text-slate-400 disabled:opacity-40'

/**
 * The list of chats, and everything you can do to one.
 *
 * The filter goes to the server rather than filtering the loaded page, because
 * the page is thirty rows of a history that grows without bound — and because
 * the server matches on what was *said* in a chat, which the rows do not carry.
 */
export default function SessionSidebar({
  sessions,
  activeId,
  search,
  showArchived,
  isPending,
  isError,
  hasMore,
  loadingMore,
  busyId,
  onSearchChange,
  onShowArchivedChange,
  onNew,
  onOpen,
  onRename,
  onArchive,
  onDelete,
  onLoadMore,
}: SessionSidebarProps) {
  const [editingId, setEditingId] = useState<number | null>(null)
  const [draft, setDraft] = useState('')

  const commit = (id: number) => {
    const title = draft.trim()
    setEditingId(null)
    if (title) {
      onRename(id, title)
    }
  }

  const startEditing = (session: ResearchSession) => {
    setDraft(session.title ?? '')
    setEditingId(session.id)
  }

  const confirmDelete = (session: ResearchSession) => {
    const name = session.title || 'this chat'
    // Spelled out because the two halves have different fates, and a user who
    // thinks the write-up goes too will never press the button.
    const message =
      `Delete "${name}"?\n\n` +
      'Its messages and tool calls are deleted permanently. ' +
      'Notes generated from it are kept — they just stop linking back here.'
    if (window.confirm(message)) {
      onDelete(session.id)
    }
  }

  return (
    <aside className="flex w-64 shrink-0 flex-col border-r border-slate-200 bg-slate-50/60">
      <div className="space-y-2 border-b border-slate-200 p-3">
        <button
          type="button"
          onClick={onNew}
          className="w-full rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-700"
        >
          New chat
        </button>

        <label className="sr-only" htmlFor="session-search">
          Filter chats
        </label>
        <input
          id="session-search"
          type="search"
          value={search}
          placeholder="Filter chats…"
          onChange={(event) => onSearchChange(event.target.value)}
          className="w-full rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs text-slate-900 outline-none focus:border-slate-500"
        />

        <label className="flex items-center gap-2 text-[11px] text-slate-600">
          <input
            type="checkbox"
            checked={showArchived}
            onChange={(event) => onShowArchivedChange(event.target.checked)}
            className="size-3.5 rounded border-slate-300 accent-slate-900"
          />
          Show archived
        </label>
      </div>

      <ul className="flex-1 overflow-y-auto p-2">
        <Body
          isPending={isPending}
          isError={isError}
          isEmpty={sessions.length === 0}
          searching={search.trim() !== ''}
          showArchived={showArchived}
        />

        {sessions.map((session) => {
          const busy = busyId === session.id
          return (
            <li key={session.id} className="group">
              {editingId === session.id ? (
                <input
                  autoFocus
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  onBlur={() => commit(session.id)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') {
                      commit(session.id)
                    }
                    if (event.key === 'Escape') {
                      setEditingId(null)
                    }
                  }}
                  className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                />
              ) : (
                <div
                  className={`flex items-center gap-1 rounded-md px-2 py-1.5 ${
                    session.id === activeId ? 'bg-slate-200' : 'hover:bg-slate-200/60'
                  } ${busy ? 'opacity-50' : ''}`}
                >
                  <button
                    type="button"
                    onClick={() => onOpen(session.id)}
                    onDoubleClick={() => startEditing(session)}
                    className={`flex-1 truncate text-left text-sm ${
                      session.archived ? 'text-slate-400 italic' : 'text-slate-700'
                    }`}
                    title={session.title ?? 'Untitled chat'}
                  >
                    {session.title || 'Untitled chat'}
                  </button>

                  {busy ? (
                    <span className="px-1 text-[10px] text-slate-500">Saving…</span>
                  ) : null}

                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => startEditing(session)}
                    className={`${iconButtonClass} invisible group-hover:visible hover:text-slate-700`}
                    title="Rename"
                  >
                    ✎
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => onArchive(session.id, !session.archived)}
                    className={`${iconButtonClass} invisible group-hover:visible hover:text-slate-700`}
                    title={session.archived ? 'Unarchive' : 'Archive'}
                  >
                    {session.archived ? '⤺' : '🗀'}
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => confirmDelete(session)}
                    className={`${iconButtonClass} invisible group-hover:visible hover:text-rose-600`}
                    title="Delete"
                  >
                    ✕
                  </button>
                </div>
              )}
            </li>
          )
        })}

        {hasMore ? (
          <li className="pt-2">
            <button
              type="button"
              disabled={loadingMore}
              onClick={onLoadMore}
              className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-xs text-slate-600 disabled:opacity-50"
            >
              {loadingMore ? 'Loading…' : 'Load more'}
            </button>
          </li>
        ) : null}
      </ul>
    </aside>
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
    return <li className="px-2 py-6 text-center text-xs text-slate-500">Loading chats…</li>
  }
  if (isError) {
    return (
      <li className="px-2 py-6 text-center text-xs text-rose-600">
        Could not load chats. Is the backend running?
      </li>
    )
  }
  if (!isEmpty) {
    return null
  }
  if (searching) {
    return <li className="px-2 py-6 text-center text-xs text-slate-500">No chats match.</li>
  }
  if (showArchived) {
    return <li className="px-2 py-6 text-center text-xs text-slate-500">No archived chats.</li>
  }
  return <li className="px-2 py-6 text-center text-xs text-slate-500">No chats yet.</li>
}
