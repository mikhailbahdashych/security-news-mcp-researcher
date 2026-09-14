import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type ReactNode } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import {
  deleteNote,
  exportUrl,
  fetchNote,
  noteQueryKey,
  notesQueryKey,
  updateNote,
  type Note,
  type NoteSource,
} from '../api/notes'
import ConfirmDialog from '../components/notes/ConfirmDialog'
import NoteArticle from '../components/notes/NoteArticle'
import { formatNoteDate, formatNoteDay } from '../components/notes/noteDate'
import Button from '../components/ui/Button'
import Card from '../components/ui/Card'
import EmptyState from '../components/ui/EmptyState'
import Input from '../components/ui/Input'
import PageHeader from '../components/ui/PageHeader'
import SectionLabel from '../components/ui/SectionLabel'
import Textarea from '../components/ui/Textarea'
import { CARD, FIELD_LABEL, buttonClass, cx } from '../components/ui/classes'
import type { EmbeddablePageProps } from '../components/ui/PageHost'
import Page from './Page'

export interface NoteDetailPageProps extends EmbeddablePageProps {
  /** Which note to show when embedded — there is no route to read it from. */
  noteId?: number
  /** How "back to the list" works when embedded. */
  onBack?: () => void
}

export default function NoteDetailPage({
  embedded = false,
  noteId: embeddedNoteId,
  onBack,
}: NoteDetailPageProps) {
  const params = useParams<{ id: string }>()
  const noteId = embedded ? (embeddedNoteId ?? Number.NaN) : Number(params.id)
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const goBack = () => {
    if (onBack) {
      onBack()
    } else {
      navigate('/notes')
    }
  }
  const backLink = <BackLink embedded={embedded} onBack={goBack} />

  const [editing, setEditing] = useState(false)
  const [draftTitle, setDraftTitle] = useState('')
  const [draftBody, setDraftBody] = useState('')
  const [copied, setCopied] = useState<string | null>(null)
  const [confirmingDelete, setConfirmingDelete] = useState(false)

  const note = useQuery({
    queryKey: noteQueryKey(noteId),
    queryFn: () => fetchNote(noteId),
    enabled: Number.isFinite(noteId),
  })

  const save = useMutation({
    mutationFn: (patch: { title?: string; body_md?: string }) => updateNote(noteId, patch),
    onSuccess: async () => {
      setEditing(false)
      await queryClient.invalidateQueries({ queryKey: noteQueryKey(noteId) })
      await queryClient.invalidateQueries({ queryKey: notesQueryKey })
    },
  })

  const remove = useMutation({
    mutationFn: () => deleteNote(noteId),
    onSuccess: async () => {
      setConfirmingDelete(false)
      await queryClient.invalidateQueries({ queryKey: notesQueryKey })
      goBack()
    },
  })

  if (note.isPending) {
    return (
      <Page width="note">
        {backLink}
        <EmptyState icon="spinner" title="Loading note…" />
      </Page>
    )
  }
  if (note.isError || !note.data) {
    return (
      <Page width="note">
        {backLink}
        <EmptyState
          tone="error"
          icon="warning"
          title="This note could not be loaded."
          description="It may have been deleted."
        />
      </Page>
    )
  }

  const data: Note = note.data

  const startEditing = () => {
    setDraftTitle(data.title ?? '')
    setDraftBody(data.body_md)
    setEditing(true)
  }

  const copy = async () => {
    // Only available on a secure origin; say so rather than silently failing.
    if (!navigator.clipboard?.writeText) {
      setCopied('Clipboard unavailable — use Download instead.')
      return
    }
    try {
      await navigator.clipboard.writeText(data.body_md)
      setCopied('Copied')
    } catch {
      setCopied('Could not copy — use Download instead.')
    }
    window.setTimeout(() => setCopied(null), 2500)
  }

  // While editing, the heading follows the draft: the title field lives in the
  // card below, and a stale heading above it would be the only thing on the page
  // disagreeing with what is being typed.
  const heading = editing ? draftTitle.trim() || `Note ${data.id}` : (data.title ?? `Note ${data.id}`)

  return (
    <Page width="note">
      <PageHeader
        back={backLink}
        title={heading}
        subtitle={<Meta note={data} />}
        actions={
          editing ? null : (
            <>
              <Button onClick={startEditing}>Edit</Button>
              <Button onClick={() => void copy()}>Copy</Button>
              {/* A plain link, so the browser honours the attachment header. */}
              <a href={exportUrl(data.id)} download className={buttonClass('secondary')}>
                Download
              </a>
              <Button
                className="text-muted hover:border-red hover:text-red"
                onClick={() => setConfirmingDelete(true)}
              >
                Delete
              </Button>
            </>
          )
        }
      />

      {copied ? <p className="text-[11.5px] text-faint">{copied}</p> : null}

      {editing ? (
        <div className={cx(CARD, 'flex flex-col gap-3 px-6 py-5')}>
          <label className="flex flex-col gap-1.5">
            <span className={FIELD_LABEL}>Title</span>
            <Input
              value={draftTitle}
              onChange={(event) => setDraftTitle(event.target.value)}
              placeholder={`Note ${data.id}`}
              tone="bg"
            />
          </label>
          <label className="flex flex-col gap-1.5">
            <span className={FIELD_LABEL}>Body (Markdown)</span>
            <Textarea
              value={draftBody}
              onChange={(event) => setDraftBody(event.target.value)}
              spellCheck={false}
              mono
              tone="bg"
              className="min-h-[50vh]"
            />
          </label>
          {save.isError ? <p className="text-[12px] text-red">The edit could not be saved.</p> : null}
          <div className="flex items-center justify-end gap-2">
            <Button onClick={() => setEditing(false)} disabled={save.isPending}>
              Cancel
            </Button>
            <Button
              variant="primary"
              loading={save.isPending}
              disabled={!draftBody.trim()}
              onClick={() =>
                save.mutate({
                  title: draftTitle.trim() || undefined,
                  body_md: draftBody,
                })
              }
            >
              Save
            </Button>
          </div>
        </div>
      ) : (
        <article className={cx(CARD, 'px-6 py-5')}>
          <NoteArticle>{data.body_md}</NoteArticle>
        </article>
      )}

      <Sources sources={data.sources} />

      {confirmingDelete ? (
        <ConfirmDialog
          title="Delete note"
          message="This note and its sources will be deleted. This cannot be undone."
          confirmLabel="Delete note"
          busy={remove.isPending}
          error={remove.isError ? 'The note could not be deleted.' : null}
          onCancel={() => setConfirmingDelete(false)}
          onConfirm={() => remove.mutate()}
        />
      ) : null}
    </Page>
  )
}

/** `<date> · edited <date> · research session` — the line under the title. */
function Meta({ note }: { note: Note }) {
  return (
    <span className="text-faint" title={formatNoteDate(note.created_at)}>
      {formatNoteDay(note.created_at)}
      {note.updated_at !== note.created_at ? (
        <span title={formatNoteDate(note.updated_at)}> · edited {formatNoteDay(note.updated_at)}</span>
      ) : null}
      {note.session_id !== null ? (
        <>
          {' · '}
          <Link
            to={`/chat/${note.session_id}`}
            className="text-muted underline underline-offset-2 hover:text-ink"
          >
            research session
          </Link>
        </>
      ) : null}
    </span>
  )
}

function Sources({ sources }: { sources: NoteSource[] }) {
  if (sources.length === 0) {
    return null
  }
  const fromInbox = sources.filter((source) => source.feed_item_id !== null)
  const fetched = sources.filter((source) => source.feed_item_id === null)

  return (
    <Card tone="panel2">
      <SectionLabel as="h2" className="text-muted">
        Sources
      </SectionLabel>
      <SourceList label="From the inbox" sources={fromInbox} />
      <SourceList label="Fetched while writing" sources={fetched} />
    </Card>
  )
}

function SourceList({ label, sources }: { label: string; sources: NoteSource[] }) {
  if (sources.length === 0) {
    return null
  }
  return (
    <div className="mt-2.5">
      <p className="text-[11px] font-medium text-faint">{label}</p>
      <ul className="mt-1 space-y-[3px]">
        {sources.map((source) => (
          <li key={source.id} className="text-[12px]">
            {source.url ? (
              <a
                href={source.url}
                target="_blank"
                rel="noopener noreferrer"
                className="underline underline-offset-2"
              >
                {source.title || source.url}
              </a>
            ) : (
              <span className="text-muted">{source.title ?? 'Untitled source'}</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

/** A link when there is a route behind it, a button when the pane owns the state. */
function BackLink({ embedded, onBack }: { embedded: boolean; onBack: () => void }): ReactNode {
  const className =
    'self-start text-[12px] text-muted no-underline transition-colors duration-150 ' +
    'hover:text-ink hover:opacity-100'
  if (embedded) {
    return (
      <button type="button" onClick={onBack} className={className}>
        ← All notes
      </button>
    )
  }
  return (
    <Link to="/notes" className={className}>
      ← All notes
    </Link>
  )
}
