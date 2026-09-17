import { groupByDay, type DayGroup } from '../lib/dates'
import type { BadgeTone } from '../components/ui/Badge'
import { apiGet, apiPatch, apiPost } from './client'
import { hostOf } from '../lib/urls'

/**
 * The knowledge base, as the SPA sees it.
 *
 * Field names mirror `backend/app/schemas/kb.py` exactly — they are checked by
 * eye, not by codegen — and the derivations the page needs (what dates a row,
 * what a hit's marker says, which chips a row gets) live here rather than in the
 * components, so the timeline and the entry page cannot disagree about a row.
 */

export const ENTRY_KINDS = ['article', 'note', 'finding', 'manual'] as const

export type EntryKind = (typeof ENTRY_KINDS)[number]
export type Authorship = 'source' | 'human' | 'model'
export type ReviewStatus = 'unreviewed' | 'reviewed'
export type CapturedBy = 'auto' | 'user'
/** How the search found a row. `entity` is the exact-identifier leg. */
export type MatchedBy = 'keyword' | 'vector' | 'both' | 'entity'

export interface KbEntity {
  /** `cve`, `vendor`, `product` — free text on the wire, so never switched on. */
  kind: string
  value: string
  /** `regex`, `model` or `user`. */
  source: string
}

export interface KbTopicRef {
  id: number
  name: string
  color: string | null
  suggested: boolean
}

export interface KbTagRef {
  tag: string
  suggested: boolean
}

/**
 * What the entry was captured *from*.
 *
 * All three are `ON DELETE SET NULL`, so a deleted note leaves the entry alive
 * with an empty `note_id` — a back-link that is gone, not a broken one.
 */
export interface KbLinks {
  feed_item_id: number | null
  note_id: number | null
  session_id: number | null
}

export interface KbEntry {
  id: number
  kind: EntryKind
  title: string
  url: string | null
  source_name: string | null
  authorship: Authorship
  lang: string | null
  published_at: string | null
  captured_at: string
  updated_at: string
  deleted_at: string | null
  review_status: ReviewStatus
  captured_by: CapturedBy
  snapshot_chars: number
  snapshot_version: number
  summary_md: string | null
  notes_md: string
  compiled_at: string | null
  entities: KbEntity[]
  topics: KbTopicRef[]
  tags: KbTagRef[]
  links: KbLinks
  possible_duplicate_of: number | null
  chunks: number
  pending_chunks: number
}

export interface KbSnapshotVersion {
  version: number
  fetched_at: string
  chars: number
  sha256: string
}

export interface KbActivity {
  id: number
  at: string
  action: string
  entry_id: number | null
  source: string | null
  model: string | null
  input_tokens: number
  output_tokens: number
  detail: string | null
}

export interface KbEntryDetail extends KbEntry {
  snapshot_md: string
  versions: KbSnapshotVersion[]
  activity: KbActivity[]
}

export interface KbHit {
  entry: KbEntry
  snippet: string
  score: number
  matched_by: MatchedBy
}

/**
 * A page of `GET /entries` — one leg, never both.
 *
 * The backend *drops* the absent key rather than sending `null`, so an `entries`
 * of `undefined` means "this answer was a search", not "the list is empty".
 */
export interface KbEntryPage {
  entries?: KbEntry[]
  hits?: KbHit[]
  /** The list branch's alone: a search is ordered by score, so it has no page
   *  after this one and the backend drops the key with the absent leg. */
  next_cursor?: string | null
}

export interface KbSearchResponse {
  hits: KbHit[]
  /** `keyword` until an embedder is configured; `hybrid` from Phase 2. */
  mode: 'keyword' | 'hybrid'
}

export interface KbRefreshResult {
  entry: KbEntry
  changed: boolean
  version: number
}

export interface KbIndexStatus {
  vec_version: string | null
  fts5: boolean
  outdated: boolean
  reasons: string[]
}

export interface KbStats {
  entries: number
  deleted: number
  chunks: number
  pending_chunks: number
  entities: number
  topics: number
  index: KbIndexStatus
  embeddings_configured: boolean
}

export interface KbTopic {
  id: number
  name: string
  description: string | null
  color: string | null
  created_at: string
  entry_count: number
}

/** Exactly one of the two sources, never both — the API answers 422 otherwise. */
export interface EntryCreate {
  feed_item_id?: number
  url?: string
  title?: string
}

export interface EntryPatch {
  title?: string
  notes_md?: string
  summary_md?: string
  review_status?: ReviewStatus
}

export interface SearchBody {
  q: string
  kinds?: EntryKind[]
  topic_ids?: number[]
  /** `"cve:CVE-2024-3094"` — the exact leg, ahead of both query legs. */
  entity?: string
  since?: string
  reviewed_only?: boolean
  limit?: number
}

export interface EntryFilters {
  kind: EntryKind | null
  topicId: number | null
  /** `"cve:CVE-2026-60004"` — already qualified, from `entityFilter`. */
  entity: string | null
  /** Naive-UTC ISO string, or null for "everything". */
  since: string | null
  /** The trash view, which is what Undo reads. */
  deleted?: boolean
}

export const KB_PAGE_SIZE = 50
export const KB_SEARCH_LIMIT = 20
/** Below this the timeline stands; a one-letter FTS query is all noise. */
export const KB_MIN_SEARCH_CHARS = 2
/** How many soft-deleted entries the "Needs attention" strip offers Undo for. */
export const KB_DELETED_LIMIT = 10

/** One prefix, so a capture or a delete can refresh every filtered variant. */
export const kbQueryKey = ['kb'] as const
export const kbEntriesKey = (filters: EntryFilters) =>
  [
    'kb',
    'entries',
    filters.kind,
    filters.topicId,
    filters.entity,
    filters.since,
    filters.deleted === true,
  ] as const
export const kbSearchKey = (q: string, filters: EntryFilters) =>
  ['kb', 'search', q, filters.kind, filters.topicId, filters.entity, filters.since] as const
export const kbEntryKey = (id: number) => ['kb', 'entry', id] as const
export const kbStatsKey = ['kb', 'stats'] as const
export const kbTopicsKey = ['kb', 'topics'] as const

function entryParams(filters: EntryFilters, limit: number, cursor?: string): string {
  const params = new URLSearchParams({ limit: String(limit) })
  if (filters.kind) {
    params.set('kind', filters.kind)
  }
  if (filters.topicId !== null) {
    params.set('topic_id', String(filters.topicId))
  }
  if (filters.entity) {
    params.set('entity', filters.entity)
  }
  if (filters.since) {
    params.set('since', filters.since)
  }
  if (filters.deleted) {
    params.set('deleted', 'true')
  }
  if (cursor) {
    params.set('cursor', cursor)
  }
  return params.toString()
}

/** One keyset page of the timeline. Never a search: `q` belongs to `searchEntries`. */
export async function listEntries(
  filters: EntryFilters,
  cursor?: string,
  limit = KB_PAGE_SIZE,
): Promise<KbEntryPage> {
  const page = await apiGet<KbEntryPage>(`/kb/entries?${entryParams(filters, limit, cursor)}`)
  // Absent rather than null when the answer came back from the search branch;
  // one shape out of here means one thing for `getNextPageParam` to read.
  return { ...page, next_cursor: page.next_cursor ?? null }
}

/**
 * The search box.
 *
 * `POST /kb/search` rather than `GET /kb/entries?q=`: the same hits either way,
 * but this one also says which mode answered (`keyword` in Phase 1, `hybrid`
 * once an embedder is configured), which is the difference the page has to be
 * able to explain.
 */
export const searchEntries = (body: SearchBody): Promise<KbSearchResponse> =>
  apiPost<KbSearchResponse>('/kb/search', body)

export const getEntry = (id: number): Promise<KbEntryDetail> =>
  apiGet<KbEntryDetail>(`/kb/entries/${id}`)

/** 201 for a new entry, 200 for one already held — the page says "Saved" to both. */
export const createEntry = (body: EntryCreate): Promise<KbEntry> =>
  apiPost<KbEntry>('/kb/entries', body)

export const patchEntry = (id: number, patch: EntryPatch): Promise<KbEntry> =>
  apiPatch<KbEntry>(`/kb/entries/${id}`, patch)

/** Soft: the chunks go, the snapshots stay, and `undeleteEntry` brings it back. */
export const deleteEntry = (id: number): Promise<KbEntry> =>
  apiPost<KbEntry>(`/kb/entries/${id}/delete`)

export const undeleteEntry = (id: number): Promise<KbEntry> =>
  apiPost<KbEntry>(`/kb/entries/${id}/undelete`)

export const refreshEntry = (id: number): Promise<KbRefreshResult> =>
  apiPost<KbRefreshResult>(`/kb/entries/${id}/refresh`)

export const getStats = (): Promise<KbStats> => apiGet<KbStats>('/kb/stats')

export const listTopics = (): Promise<KbTopic[]> => apiGet<KbTopic[]>('/kb/topics')

// ------------------------------------------------------------- derivations

/** Where an entry lives. The rail, the timeline and the detail page agree here. */
export const kbEntryLink = (id: number): string => `/knowledge/${id}`

/**
 * `/knowledge/:id`, parsed rather than `Number()`d.
 *
 * `Number('abc')` is `NaN` and `Number('1.5')` is `1.5`; both reach the API,
 * which answers **422** — a status the 404 path cannot act on, so the page sits
 * on a dead URL. Ids are SQLite rowids: all digits, and positive.
 */
export function parseEntryId(raw: string | undefined): number | null {
  if (raw === undefined || !/^\d+$/.test(raw)) {
    return null
  }
  const id = Number(raw)
  return Number.isSafeInteger(id) && id > 0 ? id : null
}

/**
 * The date an entry sorts and groups by.
 *
 * The backend orders on `COALESCE(published_at, captured_at)`, so the timeline
 * has to date every row by the same expression — otherwise a row lands under a
 * header it did not sort into.
 */
export const entryTimestamp = (entry: KbEntry): string => entry.published_at ?? entry.captured_at

/** The timeline, cut into days on the date the server ordered it by. */
export const groupEntriesByDay = (entries: KbEntry[]): DayGroup<KbEntry>[] =>
  groupByDay(entries, entryTimestamp)

export interface MatchMarker {
  label: string
  tone: BadgeTone
  title: string
}

const MARKERS: Record<MatchedBy, MatchMarker> = {
  keyword: {
    label: 'keyword',
    tone: 'neutral',
    title: 'Matched the words in the text.',
  },
  vector: {
    label: 'vector',
    tone: 'neutral',
    title: 'Matched on meaning rather than on the words.',
  },
  both: {
    label: 'both',
    tone: 'neutral',
    title: 'Matched on the words and on the meaning.',
  },
  entity: {
    label: 'exact',
    tone: 'accent',
    title: 'An exact identifier match — a CVE id, a vendor or a product.',
  },
}

/**
 * The small marker beside a hit: `keyword` / `vector` / `both` / `exact`.
 *
 * An unknown value from a newer backend is still shown, verbatim and untoned: it
 * is a real hit, and hiding the row to avoid admitting we have no word for its
 * leg would be the worse answer.
 */
export const matchMarker = (matchedBy: MatchedBy): MatchMarker =>
  MARKERS[matchedBy] ?? { label: matchedBy, tone: 'neutral', title: 'Matched the search.' }

/**
 * The passage under a hit — or nothing, when it is the title again.
 *
 * An entity hit has no chunk of its own, so the backend falls its snippet back
 * to the entry's title, and the same sentence twice in a row reads as a
 * rendering bug. Plain text, always: it is rendered by splitting it in React,
 * never as HTML.
 */
export function hitSnippet(hit: KbHit): string {
  const snippet = hit.snippet.trim()
  return snippet === hit.entry.title.trim() ? '' : snippet
}

/**
 * The CVE ids on a row, deduplicated and capped.
 *
 * The same id can arrive twice — once from the capture-time regex and once from
 * a compile — and a row is a headline, not an inventory: past `max` the rest
 * become a `+N more` chip, which the entry page then lists in full.
 */
export function cveChips(entry: KbEntry, max = 4): string[] {
  const seen: string[] = []
  for (const entity of entry.entities) {
    if (entity.kind === 'cve' && !seen.includes(entity.value)) {
      seen.push(entity.value)
    }
  }
  return seen.length > max ? [...seen.slice(0, max), `+${seen.length - max} more`] : seen
}

/**
 * What the Settings panel says beside "Vector extension".
 *
 * The backend reports a missing `vec_version()` as the **empty string**, not
 * `null` (`app/db/engine.py::extension_status` catches the `OperationalError`
 * and returns `""`, typed `str`), so `?? 'not loaded'` never fired and the row
 * drew its label with nothing beside it. `undefined` is a backend old enough not
 * to send the field at all.
 */
export const vecVersionLabel = (version: string | null | undefined): string =>
  version && version.trim() ? version : 'not loaded'

/** Who published it: the recorded name, else the host, else nothing at all. */
export const sourceLabel = (entry: KbEntry): string =>
  entry.source_name ?? hostOf(entry.url) ?? ''

const KIND_LABELS: Record<EntryKind, string> = {
  article: 'article',
  note: 'note',
  // "manual" is the API's word for the capture path (a pasted URL); "saved" is
  // the user's word for what they did.
  manual: 'saved',
  // Phase 2 captures these. The page has to be able to draw one before then,
  // because an entry can also be constructed by hand.
  finding: 'AI finding',
}

export const kindLabel = (kind: EntryKind): string => KIND_LABELS[kind] ?? kind

/** A bare CVE id, as people paste it out of an advisory. */
const CVE_ID = /^cve-\d{4}-\d{4,}$/i

/**
 * The entity box's text, as the API's `kind:value` — or `null` for no filter.
 *
 * `parse_entity` wants the qualifier and reads anything without a `:` as *no
 * filter at all*, so an unqualified word must not be sent: it would silently
 * widen the search instead of narrowing it. A bare CVE id is the one thing
 * qualified for the user, because it is what they have in the clipboard.
 */
export function entityFilter(raw: string): string | null {
  const text = raw.trim()
  if (!text) {
    return null
  }
  if (CVE_ID.test(text)) {
    return `cve:${text.toUpperCase()}`
  }
  const [kind, ...rest] = text.split(':')
  const value = rest.join(':').trim()
  if (!kind.trim() || !value) {
    return null
  }
  return `${kind.trim().toLowerCase()}:${value}`
}

/** `since` for "the last N days", as the naive-UTC string the API expects. */
export function sinceDaysAgo(days: number, now: Date = new Date()): string {
  const at = new Date(now.getTime() - days * 24 * 60 * 60 * 1000)
  return at.toISOString().replace(/\.\d+Z$/, '')
}
