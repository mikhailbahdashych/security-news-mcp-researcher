import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'

import {
  deleteNote,
  fetchNotes,
  notesListKey,
  notesQueryKey,
  type NoteSummary,
} from '../api/notes'
import type { EmbeddablePageProps } from '../components/ui/PageHost'
import useDebouncedValue from '../components/inbox/useDebouncedValue'
import GenerateNotesDialog from '../components/notes/GenerateNotesDialog'
import { formatNoteDate } from '../components/notes/noteDate'
import NoteDetailPage from './NoteDetail'

const primaryButtonClass =
  'rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800 ' +
  'disabled:bg-slate-300'

const secondaryButtonClass =
  'rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 ' +
  'hover:border-slate-400 disabled:opacity-40'

export default function NotesPage({ embedded = false }: EmbeddablePageProps) {
  const queryClient = useQueryClient()
  const [search, setSearch] = useState('')
  const [dialogOpen, setDialogOpen] = useState(false)
  // Embedded there is no `/notes/:id` to go to — the detail view opens in place.
  const [openNoteId, setOpenNoteId] = useState<number | null>(null)
  const debouncedSearch = useDebouncedValue(search)

  const notes = useInfiniteQuery({
    queryKey: notesListKey(debouncedSearch),
    queryFn: ({ pageParam }) => fetchNotes(debouncedSearch, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  })

  const remove = useMutation({
    mutationFn: (id: number) => deleteNote(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: notesQueryKey }),
  })

  const rows = notes.data?.pages.flatMap((page) => page.notes) ?? []

  if (embedded && openNoteId !== null) {
    return <NoteDetailPage embedded noteId={openNoteId} onBack={() => setOpenNoteId(null)} />
  }

  return (
    <section className="mx-auto flex max-w-4xl flex-col gap-4 px-8 py-8">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-900">Notes</h1>
          <p className="mt-0.5 text-xs text-slate-500">
            Meeting notes generated from starred items and research sessions.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setDialogOpen(true)}
          className={primaryButtonClass}
        >
          Generate notes
        </button>
      </header>

      <input
        value={search}
        onChange={(event) => setSearch(event.target.value)}
        placeholder="Search titles and bodies"
        className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
      />

      <div className="rounded-lg border border-slate-200 bg-white">
        <Body
          isPending={notes.isPending}
          isError={notes.isError}
          isEmpty={rows.length === 0}
          searching={debouncedSearch.trim() !== ''}
        />

        <ul className="divide-y divide-slate-100">
          {rows.map((note) => (
            <NoteRow
              key={note.id}
              note={note}
              busy={remove.isPending}
              onOpen={embedded ? () => setOpenNoteId(note.id) : undefined}
              onDelete={() => {
                if (window.confirm(`Delete "${note.title ?? 'this note'}"? This cannot be undone.`)) {
                  remove.mutate(note.id)
                }
              }}
            />
          ))}
        </ul>
      </div>

      {notes.hasNextPage ? (
        <button
          type="button"
          disabled={notes.isFetchingNextPage}
          onClick={() => void notes.fetchNextPage()}
          className={`${secondaryButtonClass} self-center`}
        >
          {notes.isFetchingNextPage ? 'Loading…' : 'Load more'}
        </button>
      ) : null}

      {dialogOpen ? <GenerateNotesDialog onClose={() => setDialogOpen(false)} /> : null}
    </section>
  )
}

interface NoteRowProps {
  note: NoteSummary
  busy: boolean
  onDelete: () => void
  /** Set when there is no route to link to: opens the note in place instead. */
  onOpen?: () => void
}

function NoteRow({ note, busy, onDelete, onOpen }: NoteRowProps) {
  const titleClass = 'text-sm font-medium text-slate-900 underline-offset-2 hover:underline'
  return (
    <li className="flex items-start gap-3 px-4 py-3.5">
      <div className="min-w-0 flex-1">
        {onOpen ? (
          <button type="button" onClick={onOpen} className={`block text-left ${titleClass}`}>
            {note.title ?? `Note ${note.id}`}
          </button>
        ) : (
          <Link to={`/notes/${note.id}`} className={titleClass}>
            {note.title ?? `Note ${note.id}`}
          </Link>
        )}
        <p className="mt-0.5 text-[11px] text-slate-500">
          {formatNoteDate(note.created_at)} · {note.source_count}{' '}
          {note.source_count === 1 ? 'source' : 'sources'}
          {note.session_id !== null ? ' · from a research session' : ''}
        </p>
        {/* Plain text: the excerpt is raw Markdown and is shown as such. */}
        <p className="mt-1.5 line-clamp-2 text-xs leading-relaxed text-slate-600">
          {note.excerpt}
        </p>
      </div>
      <button
        type="button"
        disabled={busy}
        onClick={onDelete}
        className="rounded border border-slate-200 px-2 py-1 text-[11px] font-medium text-slate-600 hover:border-rose-300 hover:text-rose-700 disabled:opacity-40"
      >
        Delete
      </button>
    </li>
  )
}

interface BodyProps {
  isPending: boolean
  isError: boolean
  isEmpty: boolean
  searching: boolean
}

/** The states the list can be in before it has rows to show. */
function Body({ isPending, isError, isEmpty, searching }: BodyProps) {
  if (isPending) {
    return <p className="px-4 py-10 text-center text-xs text-slate-500">Loading notes…</p>
  }
  if (isError) {
    return (
      <p className="px-4 py-10 text-center text-xs text-rose-600">
        Could not load notes. Is the backend running?
      </p>
    )
  }
  if (!isEmpty) {
    return null
  }
  if (searching) {
    return <p className="px-4 py-10 text-center text-xs text-slate-500">No notes match.</p>
  }
  return (
    <div className="px-4 py-10 text-center">
      <p className="text-xs text-slate-500">No notes yet.</p>
      <p className="mt-1 text-xs text-slate-500">
        Star a few items in the{' '}
        <Link to="/" className="underline underline-offset-2 hover:text-slate-900">
          Inbox
        </Link>
        , then generate notes from them.
      </p>
    </div>
  )
}
