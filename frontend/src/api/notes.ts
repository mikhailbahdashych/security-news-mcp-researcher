import { apiDelete, apiGet, apiPatch, apiPost } from './client'

/** One citation on a note. A null `feed_item_id` means the model fetched it. */
export interface NoteSource {
  id: number
  feed_item_id: number | null
  url: string | null
  title: string | null
}

/** A note in full — what the detail page renders and edits. */
export interface Note {
  id: number
  title: string | null
  body_md: string
  template_used: string | null
  session_id: number | null
  created_at: string
  updated_at: string
  sources: NoteSource[]
}

/** A row in the notes list. The body is represented by `excerpt` only. */
export interface NoteSummary {
  id: number
  title: string | null
  created_at: string
  updated_at: string
  session_id: number | null
  source_count: number
  excerpt: string
}

export interface NotePage {
  notes: NoteSummary[]
  next_cursor: string | null
}

export interface GenerateNotesBody {
  item_ids?: number[]
  session_id?: number | null
  title?: string
  template_override?: string
  generation_id: string
}

/** The terminal `done` payload: present only when a note was actually saved. */
export interface NoteDonePayload {
  note_id: number
}

export const NOTE_PAGE_SIZE = 30

export const notesQueryKey = ['notes'] as const
export const notesListKey = (q: string) => ['notes', 'list', q] as const
export const noteQueryKey = (id: number) => ['note', id] as const

export function fetchNotes(q: string, cursor?: string): Promise<NotePage> {
  const params = new URLSearchParams({ limit: String(NOTE_PAGE_SIZE) })
  if (q.trim()) {
    params.set('q', q.trim())
  }
  if (cursor) {
    params.set('cursor', cursor)
  }
  return apiGet<NotePage>(`/notes?${params.toString()}`)
}

export const fetchNote = (id: number): Promise<Note> => apiGet<Note>(`/notes/${id}`)

export const updateNote = (id: number, patch: { title?: string; body_md?: string }): Promise<Note> =>
  apiPatch<Note>(`/notes/${id}`, patch)

export const deleteNote = (id: number): Promise<void> => apiDelete<void>(`/notes/${id}`)

export const cancelGeneration = (generationId: string): Promise<{ cancelled: boolean }> =>
  apiPost<{ cancelled: boolean }>('/notes/generate/cancel', { generation_id: generationId })

export const generateUrl = (): string => '/api/notes/generate'

/**
 * The download URL.
 *
 * Navigated to rather than fetched, so the browser honours the attachment
 * header — and so the bytes it saves are the same bytes Copy puts on the
 * clipboard, because both come from `body_md`.
 */
export const exportUrl = (id: number): string => `/api/notes/${id}/export.md`
