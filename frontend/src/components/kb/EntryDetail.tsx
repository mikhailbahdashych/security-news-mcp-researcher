import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { conflictDetail, isNotFound } from '../../api/client'
import {
  deleteEntry,
  entityChips,
  getEntry,
  kbEntryKey,
  kbQueryKey,
  kindLabel,
  patchEntry,
  refreshEntry,
  refreshFailed,
  refreshMessage,
  sourceLabel,
  undeleteEntry,
  type KbActivity,
  type KbEntryDetail,
  type KbSnapshotVersion,
} from '../../api/kb'
import useDebouncedValue from '../../lib/useDebouncedValue'
import Markdown from '../chat/Markdown'
import { formatNoteDate, formatNoteDay } from '../notes/noteDate'
import Badge from '../ui/Badge'
import Button from '../ui/Button'
import Card from '../ui/Card'
import EmptyState from '../ui/EmptyState'
import Input from '../ui/Input'
import PageHeader from '../ui/PageHeader'
import SectionLabel from '../ui/SectionLabel'
import Textarea from '../ui/Textarea'
import { CARD, FIELD_LABEL, cx } from '../ui/classes'
import { AUTOSAVE_QUIET_MS, autosaveDecision, autosaveLabel, flushPlan } from './autosave'

export interface EntryDetailProps {
  entryId: number
  /** No router: back is a callback, and the back-links are plain text. */
  embedded: boolean
  /** `replace` when the entry left on its own — a dead id is not a place to
   *  return to, and Back onto it would only 404 forward again. */
  onBack: (replace?: boolean) => void
}

/** One entry in full: what it says, what it was captured from, what to do with it. */
export default function EntryDetail({ entryId, embedded, onBack }: EntryDetailProps) {
  const queryClient = useQueryClient()

  const entry = useQuery({
    queryKey: kbEntryKey(entryId),
    queryFn: () => getEntry(entryId),
    // A dead id must not be asked for twice: the page leaves on the 404, and the
    // app-wide `retry: 1` would make it wait out a backoff under a dead URL.
    retry: (failureCount, error) => !isNotFound(error) && failureCount < 1,
  })

  const invalidate = () => queryClient.invalidateQueries({ queryKey: kbQueryKey })

  // A hook, so it is declared above the early returns. `mutateAsync` because
  // `TitleField` has to know the write lost: it moved the field optimistically
  // and has to put it back.
  const rename = useMutation({
    mutationFn: (title: string) => patchEntry(entryId, { title }),
    onSuccess: invalidate,
  })

  const missing = entry.isError && isNotFound(entry.error)
  useEffect(() => {
    if (missing) {
      onBack(true)
    }
  }, [missing, onBack])

  const backLink = (
    <button
      type="button"
      onClick={() => onBack()}
      className="self-start text-[12px] text-muted transition-colors duration-150 hover:text-ink"
    >
      ← All knowledge
    </button>
  )

  if (entry.isPending) {
    return (
      <>
        {backLink}
        <EmptyState icon="spinner" title="Loading entry…" />
      </>
    )
  }
  if (entry.isError || !entry.data) {
    return (
      <>
        {backLink}
        <EmptyState
          tone="error"
          icon="warning"
          title="This entry could not be loaded."
          description="It may have been purged."
        />
      </>
    )
  }

  const data = entry.data
  const source = sourceLabel(data)

  return (
    <>
      <PageHeader
        back={backLink}
        title={
          <TitleField
            key={data.id}
            initial={data.title}
            failed={rename.isError}
            onCommit={rename.reset}
            onSave={rename.mutateAsync}
          />
        }
        subtitle={
          <span className="flex flex-wrap items-center gap-2 text-faint">
            <Badge>{kindLabel(data.kind)}</Badge>
            {source ? <span>{source}</span> : null}
            <span title={formatNoteDate(data.captured_at)}>
              captured {formatNoteDay(data.captured_at)}
            </span>
            {data.published_at ? (
              <span title={formatNoteDate(data.published_at)}>
                published {formatNoteDay(data.published_at)}
              </span>
            ) : null}
            <span>
              {data.snapshot_chars.toLocaleString()} chars · v{data.snapshot_version}
            </span>
            {data.url ? (
              <a href={data.url} target="_blank" rel="noopener noreferrer">
                Open the source
              </a>
            ) : null}
          </span>
        }
      />

      {/* The actions are a row of their own rather than `PageHeader`'s `actions`
          slot: the title is an editable field here, and sharing the line with
          three buttons left it a third of the column wide. */}
      <div className="flex flex-wrap items-center gap-2">
        <Actions entry={data} onChanged={invalidate} />
      </div>

      {data.deleted_at !== null ? <DeletedBanner entry={data} onChanged={invalidate} /> : null}

      {data.summary_md ? (
        <Card>
          <SectionLabel as="h2" className="text-muted">
            Summary · written by the model
          </SectionLabel>
          <div className="mt-2">
            <Markdown>{data.summary_md}</Markdown>
          </div>
        </Card>
      ) : null}

      <NotesEditor key={`notes-${data.id}`} entryId={data.id} initial={data.notes_md} />

      <Snapshot entry={data} />

      <Facts entry={data} embedded={embedded} />
    </>
  )
}

/** Save, refresh, delete — the three things you can do to a captured entry. */
function Actions({ entry, onChanged }: { entry: KbEntryDetail; onChanged: () => Promise<void> }) {
  const [note, setNote] = useState<{ text: string; failed: boolean } | null>(null)

  const refresh = useMutation({
    mutationFn: () => refreshEntry(entry.id),
    onSuccess: async (result) => {
      // "Nothing changed" is a real answer and the one the user is most often
      // checking for, so it is said out loud rather than left as a still page —
      // but a refusal is a 200 with `changed: false` too, and saying "unchanged"
      // over a 403 is the one answer that is worse than silence.
      setNote({ text: refreshMessage(result), failed: refreshFailed(result) })
      await onChanged()
    },
    onError: () => setNote({ text: 'Could not re-read the source.', failed: true }),
  })

  const remove = useMutation({
    mutationFn: () => deleteEntry(entry.id),
    onSuccess: onChanged,
  })

  if (entry.deleted_at !== null) {
    return null
  }

  return (
    <>
      {note ? (
        <span className={cx('text-[11.5px]', note.failed ? 'text-red' : 'text-faint')}>
          {note.text}
        </span>
      ) : null}
      <Button
        icon="refresh"
        loading={refresh.isPending}
        // A note or a hand-typed entry has no source to go back to.
        disabled={!entry.url}
        title={entry.url ? 'Fetch the source again' : 'This entry has no source URL'}
        onClick={() => refresh.mutate()}
      >
        Refresh
      </Button>
      <Button
        className="text-muted hover:border-red hover:text-red"
        loading={remove.isPending}
        onClick={() => remove.mutate()}
      >
        Delete
      </Button>
    </>
  )
}

/**
 * What a soft delete looks like from the entry's own page.
 *
 * The entry stays readable — that is what makes Undo possible — so the page
 * stays on screen and says what happened rather than bouncing the reader back
 * to a list with a toast they have to catch.
 */
function DeletedBanner({
  entry,
  onChanged,
}: {
  entry: KbEntryDetail
  onChanged: () => Promise<void>
}) {
  const undo = useMutation({
    mutationFn: () => undeleteEntry(entry.id),
    onSuccess: onChanged,
  })

  // A 409 is the one refusal worth spelling out — the URL was captured again
  // while this entry was in the bin, so restoring it would make two of the same
  // thing, and which survives is the reader's call. "Could not restore it."
  // over that is the one sentence that explains nothing.
  const conflict = conflictDetail(undo.error)

  return (
    <div
      className={cx(
        CARD,
        'flex flex-wrap items-center gap-3 bg-panel2 px-4 py-3 text-[12px] text-muted',
      )}
    >
      <span className="flex-1">
        Deleted {formatNoteDay(entry.deleted_at ?? entry.updated_at)}. The text is kept; the search
        index is not.
      </span>
      <Button size="sm" loading={undo.isPending} onClick={() => undo.mutate()}>
        Undo
      </Button>
      {conflict ? <span className="basis-full text-red">{conflict}</span> : null}
      {undo.isError && conflict === null ? (
        <span className="text-red">Could not restore it.</span>
      ) : null}
    </div>
  )
}

/**
 * The title, edited in place.
 *
 * Seeded once per entry through the `key`, not through an effect: a background
 * refetch lands while you are typing, and an effect that re-seeded from it would
 * throw the half-typed title away.
 */
function TitleField({
  initial,
  failed,
  onCommit,
  onSave,
}: {
  initial: string
  /** The last rename lost. Shown under the field, because the blur that would
   *  have retried it has already happened. */
  failed: boolean
  /** Clears `failed`. Called on **every** commit, including the ones that send
   *  nothing — see `commit`. */
  onCommit: () => void
  onSave: (title: string) => Promise<unknown>
}) {
  const [draft, setDraft] = useState(initial)
  const [saved, setSaved] = useState(initial)

  const commit = () => {
    // Before the early return, not after it. A failed rename puts the old name
    // back, so the next commit is usually the *unchanged* one — and that leg
    // returned without ever reaching the mutation, which left "Could not rename
    // it." under a field nobody was going to touch again.
    onCommit()
    const title = draft.trim()
    if (!title || title === saved) {
      setDraft(saved)
      return
    }
    // Optimistic, then put back if the write loses: a field left showing a name
    // the database does not have is the one outcome worse than a failed rename.
    const previous = saved
    setSaved(title)
    onSave(title).catch(() => {
      setSaved(previous)
      setDraft(previous)
    })
  }

  return (
    <>
      <Input
        aria-label="Entry title"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            event.currentTarget.blur()
          }
          if (event.key === 'Escape') {
            setDraft(saved)
          }
        }}
        className="border-transparent bg-transparent px-0 font-display text-[22px] font-semibold tracking-[-0.01em] focus:border-line"
      />
      {failed ? <span className="text-[11.5px] text-red">Could not rename it.</span> : null}
    </>
  )
}

/**
 * The notes editor: Markdown, autosaved.
 *
 * `autosaveDecision` is what decides whether to PATCH — a tested function,
 * because "is this worth saving" weighs the debounced text against the text in
 * the box and against a save that may still be in flight.
 */
function NotesEditor({ entryId, initial }: { entryId: number; initial: string }) {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState(initial)
  const [saved, setSaved] = useState(initial)
  const [savedOnce, setSavedOnce] = useState(false)
  const pending = useDebouncedValue(draft, AUTOSAVE_QUIET_MS)

  // The text a PATCH is carrying right now and the promise it is carried on.
  // Refs: the unmount flush reads both during cleanup, when the render's values
  // are gone.
  const sending = useRef<string | null>(null)
  const inFlight = useRef<Promise<unknown> | null>(null)

  const save = useMutation({
    mutationFn: (notes_md: string) => {
      const request = patchEntry(entryId, { notes_md })
      sending.current = notes_md
      inFlight.current = request
      return request
    },
    onSuccess: async (entry) => {
      setSaved(entry.notes_md)
      setSavedOnce(true)
      await queryClient.invalidateQueries({ queryKey: kbEntryKey(entryId) })
    },
    onSettled: () => {
      sending.current = null
      inFlight.current = null
    },
  })

  const decision = autosaveDecision({
    pending,
    saved,
    settled: pending === draft,
    saving: save.isPending,
    // The mutation already remembers what it was last asked to send, so the
    // text that lost needs no state of its own: `variables` is that text and
    // `isError` says it lost. Both clear themselves on the next `mutate`.
    failed: save.isError ? (save.variables ?? null) : null,
  })
  const { mutate } = save

  useEffect(() => {
    if (decision === 'save') {
      mutate(pending)
    }
  }, [decision, pending, mutate])

  // Unmounting cancels the debounce, so "type a line, click back" inside the
  // quiet window would send nothing at all. Refs because this runs during
  // cleanup, when the render's `draft` and the mutation object are both gone.
  const latest = useRef(draft)
  const confirmed = useRef(saved)
  // Updated in an effect rather than during the render: a ref written while
  // rendering is a lint error and, under a re-render React throws away, a lie.
  useEffect(() => {
    latest.current = draft
    confirmed.current = saved
  })
  useEffect(
    () => () => {
      const plan = flushPlan({
        latest: latest.current,
        confirmed: confirmed.current,
        sending: sending.current,
      })
      if (plan.text === null) {
        return
      }
      const text = plan.text
      // The cache is invalidated *after* the write lands: `staleTime` is 30 s,
      // so reopening this entry inside that window would otherwise re-seed the
      // editor from the copy this PATCH just replaced — and typing again would
      // send that stale base back over the flushed text. `queryClient` outlives
      // the component, so this is safe after unmount.
      const send = () =>
        patchEntry(entryId, { notes_md: text }).then(() =>
          queryClient.invalidateQueries({ queryKey: kbQueryKey }),
        )
      const previous = plan.afterInFlight ? inFlight.current : null
      // Either outcome of the previous PATCH is a reason to send this one: it
      // carries different text. Only the *order* matters.
      void (previous ? previous.then(send, send) : send()).catch(() => {})
    },
    [entryId, queryClient],
  )

  const label = autosaveLabel(decision, savedOnce, save.isError)

  return (
    <div className={cx(CARD, 'flex flex-col gap-2 bg-panel px-5 py-[18px]')}>
      <div className="flex items-baseline justify-between gap-3">
        <span className={FIELD_LABEL}>Your notes (Markdown)</span>
        {label ? (
          <span className={cx('text-[11px]', save.isError ? 'text-red' : 'text-faint')}>
            {label}
          </span>
        ) : null}
      </div>
      <Textarea
        aria-label="Your notes"
        tone="bg"
        rows={5}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        placeholder="Why this matters to us, what to say about it on Monday…"
      />
    </div>
  )
}

/** The captured text, and the versions of it. Collapsed: it is the raw article. */
function Snapshot({ entry }: { entry: KbEntryDetail }) {
  const [open, setOpen] = useState(false)

  return (
    <Card>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <SectionLabel as="h2" className="text-muted">
          Snapshot
        </SectionLabel>
        <Button
          size="sm"
          icon={open ? 'chevronDown' : 'chevronRight'}
          onClick={() => setOpen(!open)}
        >
          {open ? 'Hide the text' : `Show the text · ${entry.snapshot_chars.toLocaleString()} chars`}
        </Button>
      </div>

      {open ? (
        // Its own scrollport: a captured article is thousands of words, and
        // pushing the versions and the back-links below it makes them unfindable.
        <div className="mt-3 max-h-[520px] overflow-y-auto rounded-[8px] bg-bg px-4 py-3">
          {entry.snapshot_md ? (
            <Markdown>{entry.snapshot_md}</Markdown>
          ) : (
            <p className="text-[12px] text-faint">This entry has no stored text.</p>
          )}
        </div>
      ) : null}

      <Versions versions={entry.versions} />
    </Card>
  )
}

function Versions({ versions }: { versions: KbSnapshotVersion[] }) {
  if (versions.length === 0) {
    return null
  }
  return (
    <div className="mt-3">
      <p className="text-[11px] font-medium text-faint">Versions</p>
      <ul className="mt-1 space-y-[3px]">
        {versions.map((version) => (
          <li key={version.version} className="text-[11.5px] text-muted">
            <span className="font-mono">v{version.version}</span>
            <span title={formatNoteDate(version.fetched_at)}>
              {' · '}
              {formatNoteDay(version.fetched_at)}
            </span>
            {' · '}
            {version.chars.toLocaleString()} chars
            <span className="text-faint" title={version.sha256}>
              {' · '}
              {version.sha256.slice(0, 8)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

/**
 * Entities, topics, tags and back-links — read-only in Phase 1.
 *
 * Editing them is Phase 4 (topics) and Phase 2 (the compile that suggests them).
 * They are shown anyway: an entry that quietly holds three CVE ids is worth
 * seeing even before there is a way to change them.
 */
function Facts({ entry, embedded }: { entry: KbEntryDetail; embedded: boolean }) {
  // Deduplicated: the same `cve:…` arrives from the capture-time regex and from
  // a compile as two rows with different `source`, and `Chips` keys on the
  // label.
  const entities = entityChips(entry)
  const topics = entry.topics.map((topic) => (topic.suggested ? `${topic.name} (suggested)` : topic.name))
  const tags = entry.tags.map((tag) => (tag.suggested ? `${tag.tag} (suggested)` : tag.tag))

  return (
    <Card tone="panel2">
      <SectionLabel as="h2" className="text-muted">
        About this entry
      </SectionLabel>
      <div className="mt-2.5 flex flex-col gap-2.5">
        <Chips label="Entities" values={entities} empty="None found yet." />
        <Chips label="Topics" values={topics} empty="None — topics arrive with the compile step." />
        <Chips label="Tags" values={tags} empty="None." />
        <BackLinks entry={entry} embedded={embedded} />
        <Activity rows={entry.activity} />
      </div>
    </Card>
  )
}

/**
 * What has happened to this entry.
 *
 * `GET /kb/entries/{id}` carries these rows whether or not anything draws them,
 * and in Phase 1 this is the only place a capture or a refresh *failure* is
 * visible at all — the entry itself just looks short.
 */
function Activity({ rows }: { rows: KbActivity[] }) {
  if (rows.length === 0) {
    return null
  }
  return (
    <div>
      <p className="text-[11px] font-medium text-faint">Activity</p>
      <ul className="mt-1 space-y-[3px]">
        {rows.map((row) => (
          <li key={row.id} className="text-[11.5px] text-muted">
            <span className="font-mono">{row.action}</span>
            <span className="text-faint" title={formatNoteDate(row.at)}>
              {' · '}
              {formatNoteDay(row.at)}
              {row.source ? ` · ${row.source}` : ''}
            </span>
            {row.detail ? <span className="text-faint"> — {row.detail}</span> : null}
          </li>
        ))}
      </ul>
    </div>
  )
}

function Chips({ label, values, empty }: { label: string; values: string[]; empty: string }) {
  return (
    <div>
      <p className="text-[11px] font-medium text-faint">{label}</p>
      {values.length === 0 ? (
        <p className="mt-1 text-[11.5px] text-faint">{empty}</p>
      ) : (
        <div className="mt-1 flex flex-wrap gap-1">
          {values.map((value) => (
            <span
              key={value}
              className="rounded-[5px] bg-panel px-1.5 py-0.5 font-mono text-[10.5px] text-muted"
            >
              {value}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * Where this entry came from.
 *
 * Embedded there is no route to follow: a `<Link>` here changes the URL, which
 * swaps the *left* pane out from under the reader while the entry they are
 * reading stays put.
 */
function BackLinks({ entry, embedded }: { entry: KbEntryDetail; embedded: boolean }) {
  const links: { to: string; label: string }[] = []
  if (entry.links.feed_item_id !== null) {
    links.push({ to: `/?item=${entry.links.feed_item_id}&status=all`, label: 'the inbox item' })
  }
  if (entry.links.note_id !== null) {
    links.push({ to: `/notes/${entry.links.note_id}`, label: 'the note it came from' })
  }
  if (entry.links.session_id !== null) {
    links.push({ to: `/chat/${entry.links.session_id}`, label: 'the research session' })
  }

  return (
    <div>
      <p className="text-[11px] font-medium text-faint">Captured from</p>
      {links.length === 0 ? (
        <p className="mt-1 text-[11.5px] text-faint">
          {entry.captured_by === 'user' ? 'A URL you saved by hand.' : 'Nothing that still exists.'}
        </p>
      ) : (
        <ul className="mt-1 space-y-[3px]">
          {links.map((link) => (
            <li key={link.to} className="text-[11.5px]">
              {embedded ? (
                <span className="text-muted">{link.label}</span>
              ) : (
                <Link to={link.to} className="underline underline-offset-2">
                  {link.label}
                </Link>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
