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
import useDebouncedValue from '../components/inbox/useDebouncedValue'
import ConfirmDialog from '../components/notes/ConfirmDialog'
import GenerateNotesDialog from '../components/notes/GenerateNotesDialog'
import { formatNoteDate, formatNoteDay } from '../components/notes/noteDate'
import Button from '../components/ui/Button'
import Card from '../components/ui/Card'
import EmptyState from '../components/ui/EmptyState'
import IconButton from '../components/ui/IconButton'
import Input from '../components/ui/Input'
import PageHeader from '../components/ui/PageHeader'
import { HOVER_ROW, cx } from '../components/ui/classes'
import type { EmbeddablePageProps } from '../components/ui/PageHost'
import NoteDetailPage from './NoteDetail'
import Page from './Page'

export default function NotesPage({ embedded = false }: EmbeddablePageProps) {
  const queryClient = useQueryClient()
  const [search, setSearch] = useState('')
  const [dialogOpen, setDialogOpen] = useState(false)
  const [pendingDelete, setPendingDelete] = useState<NoteSummary | null>(null)
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
    onSuccess: async () => {
      setPendingDelete(null)
      await queryClient.invalidateQueries({ queryKey: notesQueryKey })
    },
  })

  const rows = notes.data?.pages.flatMap((page) => page.notes) ?? []

  if (embedded && openNoteId !== null) {
    return <NoteDetailPage embedded noteId={openNoteId} onBack={() => setOpenNoteId(null)} />
  }

  return (
    <Page>
      <PageHeader
        title="Notes"
        subtitle="Meeting notes generated from starred items and research sessions."
        actions={
          <Button variant="primary" onClick={() => setDialogOpen(true)}>
            Generate notes
          </Button>
        }
      />

      <Input
        value={search}
        onChange={(event) => setSearch(event.target.value)}
        placeholder="Search titles and bodies"
        aria-label="Search notes"
      />

      <Card padded={false}>
        <ListState
          isPending={notes.isPending}
          isError={notes.isError}
          isEmpty={rows.length === 0}
          searching={debouncedSearch.trim() !== ''}
        />

        <ul>
          {rows.map((note) => (
            <NoteRow
              key={note.id}
              note={note}
              busy={remove.isPending}
              onOpen={embedded ? () => setOpenNoteId(note.id) : undefined}
              onDelete={() => setPendingDelete(note)}
            />
          ))}
        </ul>
      </Card>

      {notes.hasNextPage ? (
        <Button
          className="self-center"
          loading={notes.isFetchingNextPage}
          onClick={() => void notes.fetchNextPage()}
        >
          Load more
        </Button>
      ) : null}

      {dialogOpen ? (
        <GenerateNotesDialog
          onClose={() => setDialogOpen(false)}
          // Routed, the dialog navigates to the new note; embedded there is no
          // route to navigate, so the pane opens it itself.
          onGenerated={embedded ? (id) => setOpenNoteId(id) : undefined}
        />
      ) : null}

      {pendingDelete ? (
        <ConfirmDialog
          title="Delete note"
          message={
            <>
              “{pendingDelete.title ?? `Note ${pendingDelete.id}`}” will be deleted, along with its
              sources. This cannot be undone.
            </>
          }
          confirmLabel="Delete note"
          busy={remove.isPending}
          error={remove.isError ? 'The note could not be deleted.' : null}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => remove.mutate(pendingDelete.id)}
        />
      ) : null}
    </Page>
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
  const sources = `${note.source_count} ${note.source_count === 1 ? 'source' : 'sources'}`
  const body = (
    <>
      <span className="block font-display text-[15px] font-semibold text-ink">
        {note.title ?? `Note ${note.id}`}
      </span>
      <span className="mt-0.5 block text-[11px] text-faint" title={formatNoteDate(note.created_at)}>
        {formatNoteDay(note.created_at)} · {sources}
        {note.session_id !== null ? ' · from a research session' : ''}
      </span>
      {/* Plain text: the excerpt is raw Markdown and is shown as such. */}
      <span className="mt-[5px] line-clamp-2 block text-[12px] leading-[1.55] text-muted">
        {note.excerpt}
      </span>
    </>
  )
  // `no-underline`/`opacity-100` undo the base `a` rules: inside a row the link
  // is the whole block, and fading it on hover fights the row's own highlight.
  const openClass = 'block min-w-0 flex-1 text-left no-underline hover:opacity-100'

  return (
    <li
      className={cx(
        'flex items-start gap-3 border-t border-line px-4 py-[13px] first:border-t-0',
        HOVER_ROW,
      )}
    >
      {onOpen ? (
        <button type="button" onClick={onOpen} className={openClass}>
          {body}
        </button>
      ) : (
        <Link to={`/notes/${note.id}`} className={openClass}>
          {body}
        </Link>
      )}
      <IconButton
        icon="dismiss"
        label="Delete note"
        tone="danger"
        disabled={busy}
        onClick={onDelete}
      />
    </li>
  )
}

interface ListStateProps {
  isPending: boolean
  isError: boolean
  isEmpty: boolean
  searching: boolean
}

/** The states the list can be in before it has rows to show. */
function ListState({ isPending, isError, isEmpty, searching }: ListStateProps) {
  if (isPending) {
    return <EmptyState icon="spinner" title="Loading notes…" />
  }
  if (isError) {
    return (
      <EmptyState
        tone="error"
        icon="warning"
        title="Could not load notes."
        description="Is the backend running?"
      />
    )
  }
  if (!isEmpty) {
    return null
  }
  if (searching) {
    return <EmptyState icon="search" title="No notes match." />
  }
  return (
    <EmptyState
      icon="notes"
      title="No notes yet."
      description={
        <>
          Star a few items in the <Link to="/">Inbox</Link>, then generate notes from them.
        </>
      }
    />
  )
}
