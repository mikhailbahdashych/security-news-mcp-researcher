import { useQuery } from '@tanstack/react-query'

import { getStats, kbStatsKey, vecVersionLabel, type KbStats } from '../../api/kb'
import type { KbSchemaVersion } from '../../api/settings'
import Checkbox from '../ui/Checkbox'
import { FIELD_HINT } from '../ui/classes'
import NumberField from './NumberField'
import SettingsSection from './SettingsSection'

export interface KnowledgeSectionProps {
  /** The Settings page's per-instance id prefix: it can be on screen twice. */
  uid: string
  captureStarred: boolean
  captureNotes: boolean
  minSnapshotChars: number
  /** Absent on a backend old enough not to report it — the row is then dropped. */
  schema: KbSchemaVersion | undefined
  onCaptureStarred: (checked: boolean) => void
  onCaptureNotes: (checked: boolean) => void
  onMinSnapshotChars: (value: number) => void
}

/**
 * Settings → Knowledge: what gets captured, and what the index holds.
 *
 * The two toggles are the whole capture policy — saving a URL by hand is always
 * allowed, and nothing else writes to the knowledge base in this phase. The
 * stats are a live read of `GET /api/kb/stats` rather than part of the settings
 * draft, because they are facts about the database, not preferences: the Save
 * button has nothing to do with them.
 */
export default function KnowledgeSection({
  uid,
  captureStarred,
  captureNotes,
  minSnapshotChars,
  schema,
  onCaptureStarred,
  onCaptureNotes,
  onMinSnapshotChars,
}: KnowledgeSectionProps) {
  const stats = useQuery({ queryKey: kbStatsKey, queryFn: getStats })

  return (
    <SettingsSection
      title="Knowledge"
      description="What the app keeps a searchable copy of, and what that index holds."
    >
      <Checkbox
        id={`${uid}-kb-capture-starred`}
        label="Capture starred items"
        hint="Starring an item in the Inbox extracts the article and keeps it."
        checked={captureStarred}
        onChange={onCaptureStarred}
      />
      <Checkbox
        id={`${uid}-kb-capture-notes`}
        label="Capture generated notes"
        hint="A note you generate is kept alongside the articles it was written from."
        checked={captureNotes}
        onChange={onCaptureNotes}
      />
      <NumberField
        id={`${uid}-kb-min-snapshot-chars`}
        label="Minimum text length (characters)"
        hint="Below this a page is a paywall or a cookie wall, not an article, and is not stored."
        className="max-w-[200px]"
        value={minSnapshotChars}
        min={0}
        max={100_000}
        onChange={onMinSnapshotChars}
      />

      <Stats stats={stats.data} isPending={stats.isPending} isError={stats.isError} schema={schema} />
    </SettingsSection>
  )
}

function Stats({
  stats,
  isPending,
  isError,
  schema,
}: {
  stats: KbStats | undefined
  isPending: boolean
  isError: boolean
  schema: KbSchemaVersion | undefined
}) {
  if (isPending) {
    return <p className={FIELD_HINT}>Reading the index…</p>
  }
  if (isError || !stats) {
    return <p className="text-[11.5px] text-red">The index could not be read.</p>
  }

  // Every read here is guarded. These are facts about a *database*, sent by a
  // backend that need not be the same version as this bundle, and one missing
  // key used to take the whole SPA down with it — there was no error boundary
  // anywhere. There is one now, and this panel no longer needs it.
  const index = stats.index as KbStats['index'] | undefined
  const rows: [string, string][] = [
    ['Entries', `${stats.entries.toLocaleString()}${stats.deleted > 0 ? ` (${stats.deleted} deleted)` : ''}`],
    [
      'Chunks',
      stats.pending_chunks > 0
        ? `${stats.chunks.toLocaleString()} · ${stats.pending_chunks.toLocaleString()} waiting for an embedding`
        : stats.chunks.toLocaleString(),
    ],
    ['Entities', stats.entities.toLocaleString()],
    ['Topics', stats.topics.toLocaleString()],
    ['Keyword index (FTS5)', index?.fts5 ? 'available' : 'missing'],
    // The vector extension is optional in Phase 1: without it the KB is a
    // keyword index, which is a working knowledge base and not a broken one.
    ['Vector extension', vecVersionLabel(index?.vec_version)],
    ['Embeddings', stats.embeddings_configured ? 'configured' : 'not configured yet'],
  ]
  if (schema) {
    rows.push(['Schema', `v${schema.version} · ${schema.vec_dimensions} dims · ${schema.tokenizer}`])
  }

  return (
    <div className="flex flex-col gap-1.5">
      <dl className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-x-4 gap-y-1">
        {rows.map(([label, value]) => (
          <div key={label} className="flex items-baseline justify-between gap-3">
            <dt className="text-[11.5px] text-faint">{label}</dt>
            <dd className="text-[11.5px] text-muted">{value}</dd>
          </div>
        ))}
      </dl>
      {index?.outdated ? (
        <p className="text-[11.5px] text-amber">
          The index was built by an older version of this app: {(index.reasons ?? []).join('; ')}. A
          rebuild arrives with the embedding step.
        </p>
      ) : null}
    </div>
  )
}
