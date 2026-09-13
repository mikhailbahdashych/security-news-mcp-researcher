import { useState } from 'react'

import type { ResearchSession } from '../../api/chat'

interface SessionSidebarProps {
  sessions: ResearchSession[]
  activeId: number | null
  hasMore: boolean
  loadingMore: boolean
  onNew: () => void
  onOpen: (id: number) => void
  onRename: (id: number, title: string) => void
  onDelete: (id: number) => void
  onLoadMore: () => void
}

export default function SessionSidebar({
  sessions,
  activeId,
  hasMore,
  loadingMore,
  onNew,
  onOpen,
  onRename,
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

  return (
    <aside className="flex w-64 shrink-0 flex-col border-r border-slate-200 bg-slate-50/60">
      <div className="border-b border-slate-200 p-3">
        <button
          type="button"
          onClick={onNew}
          className="w-full rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-700"
        >
          New chat
        </button>
      </div>

      <ul className="flex-1 overflow-y-auto p-2">
        {sessions.length === 0 ? (
          <li className="px-2 py-6 text-center text-xs text-slate-500">No chats yet.</li>
        ) : null}

        {sessions.map((session) => (
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
                }`}
              >
                <button
                  type="button"
                  onClick={() => onOpen(session.id)}
                  onDoubleClick={() => {
                    setDraft(session.title ?? '')
                    setEditingId(session.id)
                  }}
                  className="flex-1 truncate text-left text-sm text-slate-700"
                  title={session.title ?? 'Untitled chat'}
                >
                  {session.title || 'Untitled chat'}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setDraft(session.title ?? '')
                    setEditingId(session.id)
                  }}
                  className="invisible px-1 text-xs text-slate-400 group-hover:visible hover:text-slate-700"
                  title="Rename"
                >
                  ✎
                </button>
                <button
                  type="button"
                  onClick={() => {
                    if (window.confirm('Delete this chat and its transcript?')) {
                      onDelete(session.id)
                    }
                  }}
                  className="invisible px-1 text-xs text-slate-400 group-hover:visible hover:text-rose-600"
                  title="Delete"
                >
                  ✕
                </button>
              </div>
            )}
          </li>
        ))}

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
