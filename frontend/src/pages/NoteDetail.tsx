import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
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
import Markdown from '../components/chat/Markdown'
import { formatNoteDate } from '../components/notes/noteDate'

const buttonClass =
  'rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 ' +
  'hover:border-slate-400 disabled:opacity-40'

const primaryButtonClass =
  'rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800 ' +
  'disabled:bg-slate-300'

export default function NoteDetailPage() {
  const params = useParams<{ id: string }>()
  const noteId = Number(params.id)
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [editing, setEditing] = useState(false)
  const [draftTitle, setDraftTitle] = useState('')
  const [draftBody, setDraftBody] = useState('')
  const [copied, setCopied] = useState<string | null>(null)

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
      await queryClient.invalidateQueries({ queryKey: notesQueryKey })
      navigate('/notes')
    },
  })

  if (note.isPending) {
    return <Shell>Loading note…</Shell>
  }
  if (note.isError || !note.data) {
    return (
      <Shell>
        <span className="text-rose-600">
          This note could not be loaded. It may have been deleted.
        </span>
      </Shell>
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

  return (
    <section className="mx-auto flex max-w-3xl flex-col gap-4 px-8 py-8">
      <Link to="/notes" className="text-xs text-slate-500 hover:text-slate-900">
        ← All notes
      </Link>

      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          {editing ? (
            <input
              value={draftTitle}
              onChange={(event) => setDraftTitle(event.target.value)}
              aria-label="Note title"
              className="w-full rounded-md border border-slate-300 px-3 py-1.5 text-lg font-semibold text-slate-900"
            />
          ) : (
            <h1 className="text-2xl font-semibold tracking-tight text-slate-900">
              {data.title ?? `Note ${data.id}`}
            </h1>
          )}
          <p className="mt-1 text-xs text-slate-500">
            {formatNoteDate(data.created_at)}
            {data.updated_at !== data.created_at
              ? ` · edited ${formatNoteDate(data.updated_at)}`
              : ''}
            {data.session_id !== null ? (
              <>
                {' · '}
                <Link
                  to={`/chat/${data.session_id}`}
                  className="underline underline-offset-2 hover:text-slate-900"
                >
                  research session
                </Link>
              </>
            ) : null}
          </p>
        </div>

        <div className="flex flex-wrap gap-2">
          {editing ? (
            <>
              <button
                type="button"
                onClick={() => setEditing(false)}
                className={buttonClass}
                disabled={save.isPending}
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={save.isPending || !draftBody.trim()}
                onClick={() =>
                  save.mutate({
                    title: draftTitle.trim() || undefined,
                    body_md: draftBody,
                  })
                }
                className={primaryButtonClass}
              >
                {save.isPending ? 'Saving…' : 'Save'}
              </button>
            </>
          ) : (
            <>
              <button type="button" onClick={startEditing} className={buttonClass}>
                Edit
              </button>
              <button type="button" onClick={() => void copy()} className={buttonClass}>
                Copy
              </button>
              {/* A plain link, so the browser honours the attachment header. */}
              <a href={exportUrl(data.id)} download className={buttonClass}>
                Download
              </a>
              <button
                type="button"
                disabled={remove.isPending}
                onClick={() => {
                  if (window.confirm('Delete this note? This cannot be undone.')) {
                    remove.mutate()
                  }
                }}
                className={`${buttonClass} hover:border-rose-300 hover:text-rose-700`}
              >
                Delete
              </button>
            </>
          )}
        </div>
      </header>

      {copied ? <p className="text-xs text-slate-500">{copied}</p> : null}
      {save.isError ? (
        <p className="text-xs text-rose-600">The edit could not be saved.</p>
      ) : null}

      {editing ? (
        <textarea
          value={draftBody}
          onChange={(event) => setDraftBody(event.target.value)}
          aria-label="Note body"
          spellCheck={false}
          className="min-h-[60vh] w-full rounded-lg border border-slate-300 p-4 font-mono text-xs leading-relaxed text-slate-800"
        />
      ) : (
        <article className="rounded-lg border border-slate-200 bg-white px-5 py-4">
          <Markdown>{data.body_md}</Markdown>
        </article>
      )}

      <Sources sources={data.sources} />
    </section>
  )
}

function Sources({ sources }: { sources: NoteSource[] }) {
  if (sources.length === 0) {
    return null
  }
  const fromInbox = sources.filter((source) => source.feed_item_id !== null)
  const fetched = sources.filter((source) => source.feed_item_id === null)

  return (
    <section className="rounded-lg border border-slate-200 bg-slate-50 px-5 py-4">
      <h2 className="text-xs font-semibold tracking-wide text-slate-700 uppercase">Sources</h2>
      <SourceList label="From the inbox" sources={fromInbox} />
      <SourceList label="Fetched while writing" sources={fetched} />
    </section>
  )
}

function SourceList({ label, sources }: { label: string; sources: NoteSource[] }) {
  if (sources.length === 0) {
    return null
  }
  return (
    <div className="mt-3">
      <p className="text-[11px] font-medium text-slate-500">{label}</p>
      <ul className="mt-1 space-y-1">
        {sources.map((source) => (
          <li key={source.id} className="text-xs">
            {source.url ? (
              <a
                href={source.url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-sky-700 underline underline-offset-2 hover:text-sky-900"
              >
                {source.title || source.url}
              </a>
            ) : (
              <span className="text-slate-600">{source.title ?? 'Untitled source'}</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <section className="mx-auto max-w-3xl px-8 py-10">
      <Link to="/notes" className="text-xs text-slate-500 hover:text-slate-900">
        ← All notes
      </Link>
      <p className="mt-4 text-sm text-slate-600">{children}</p>
    </section>
  )
}
