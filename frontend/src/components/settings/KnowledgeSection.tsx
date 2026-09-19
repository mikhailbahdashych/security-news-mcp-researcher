import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import {
  KB_ACTIVITY_LOG_LIMIT,
  budgetLabel,
  embedPending,
  formatTokens,
  getBudget,
  getStats,
  kbActivityKey,
  kbBudgetKey,
  kbQueryKey,
  kbStatsKey,
  listActivity,
  vecVersionLabel,
  type KbActivity,
  type KbStats,
} from '../../api/kb'
import {
  COMPILE_MODES,
  EFFORTS,
  modelsQueryKey,
  settingsQueryKey,
  updateSettings,
  type AppSettings,
  type CompileMode,
  type Effort,
} from '../../api/settings'
import { formatNoteDate, formatNoteDay } from '../notes/noteDate'
import Button from '../ui/Button'
import Checkbox from '../ui/Checkbox'
import Input from '../ui/Input'
import Select, { UnknownOption } from '../ui/Select'
import Textarea from '../ui/Textarea'
import { FIELD_HINT, cx } from '../ui/classes'
import Field, { FIELD_GRID } from './Field'
import NumberField from './NumberField'
import SettingsSection from './SettingsSection'
import { embedAgain, embedProblem } from './embedNow'

/** The knowledge base's share of the settings draft. */
export interface KnowledgeDraft {
  kb_capture_starred: boolean
  kb_capture_notes: boolean
  kb_capture_findings: boolean
  kb_min_snapshot_chars: number
  kb_embedding_model: string
  kb_compile_mode: CompileMode
  kb_compile_model: string
  kb_compile_effort: Effort
  kb_compile_prompt: string
  kb_compile_max_chars: number
  kb_compile_monthly_token_budget: number
  kb_auto_accept_suggestions: boolean
  kb_reviewed_only: boolean
  kb_recency_boost: boolean
  kb_rerank: boolean
  kb_duplicate_threshold: number
}

export interface KnowledgeSectionProps {
  /** The Settings page's per-instance id prefix: it can be on screen twice. */
  uid: string
  draft: KnowledgeDraft
  /** What the server last said — the masked key, its source, the schema row. */
  settings: AppSettings
  onEdit: (patch: Partial<KnowledgeDraft>) => void
}

/**
 * Settings → Knowledge: what gets captured, what it is embedded and summarised
 * with, and what the index holds.
 *
 * Everything that is a **preference** rides in the settings draft and waits for
 * Save. Everything that is a **fact about the database** — the index counts, the
 * month's spend, the trail — is read live beside it, because the Save button has
 * nothing to do with those. Every read of those payloads is guarded field by
 * field: they come from a backend that need not be this bundle's version, and one
 * missing key used to take the whole SPA down.
 *
 * The Voyage key is its own write: like the Anthropic key it is write-only over
 * the API, read back masked, and saved on its own button rather than with the
 * rest of the form.
 */
export default function KnowledgeSection({ uid, draft, settings, onEdit }: KnowledgeSectionProps) {
  const stats = useQuery({ queryKey: kbStatsKey, queryFn: getStats })

  return (
    <SettingsSection
      title="Knowledge"
      description="What the app keeps a searchable copy of, how it is indexed, and what it costs."
    >
      <Checkbox
        id={`${uid}-kb-capture-starred`}
        label="Capture starred items"
        hint="Starring an item in the Inbox extracts the article and keeps it."
        checked={draft.kb_capture_starred}
        onChange={(checked) => onEdit({ kb_capture_starred: checked })}
      />
      <Checkbox
        id={`${uid}-kb-capture-notes`}
        label="Capture generated notes"
        hint="A note you generate is kept alongside the articles it was written from."
        checked={draft.kb_capture_notes}
        onChange={(checked) => onEdit({ kb_capture_notes: checked })}
      />
      <Checkbox
        id={`${uid}-kb-capture-findings`}
        label="Capture research findings"
        hint="A finding is the model’s own answer to one of your questions, kept as an entry. It is never given back to the chat or to note generation until you have marked it reviewed."
        checked={draft.kb_capture_findings}
        onChange={(checked) => onEdit({ kb_capture_findings: checked })}
      />
      <NumberField
        id={`${uid}-kb-min-snapshot-chars`}
        label="Minimum text length (characters)"
        hint="Below this a page is a paywall or a cookie wall, not an article, and is not stored."
        className="max-w-[200px]"
        value={draft.kb_min_snapshot_chars}
        min={0}
        max={100_000}
        onChange={(value) => onEdit({ kb_min_snapshot_chars: value })}
      />

      <VoyageKey settings={settings} uid={uid} />

      <Field
        label="Embedding model"
        htmlFor={`${uid}-kb-embedding-model`}
        hint="Changing this empties the vector index: every chunk is marked pending and has to be embedded again with the new model, because vectors from two models cannot be compared."
      >
        {/* A select, not a free-text box: the server refuses a name the embedder
            does not know (a 422), and the names it does know are on the wire so
            this list is never a second copy of them. A typo here used to be
            stored, and a stored typo is a *changed* model — which empties the
            vector index before anything checks that Voyage would accept it. */}
        <Select
          id={`${uid}-kb-embedding-model`}
          tone="bg"
          value={draft.kb_embedding_model}
          onChange={(event) => onEdit({ kb_embedding_model: event.target.value })}
          className="max-w-[260px]"
        >
          {settings.kb_embedding_models.map((model) => (
            <option key={model} value={model}>
              {model}
            </option>
          ))}
          <UnknownOption value={draft.kb_embedding_model} options={settings.kb_embedding_models} />
        </Select>
      </Field>

      <Compile uid={uid} draft={draft} settings={settings} onEdit={onEdit} />

      <Retrieval uid={uid} draft={draft} onEdit={onEdit} />

      <Stats
        stats={stats.data}
        isPending={stats.isPending}
        isError={stats.isError}
        schema={settings.kb_schema_version}
      />

      <ActivityLog />
    </SettingsSection>
  )
}

/**
 * The Voyage credential.
 *
 * Stored in the database and nowhere else, exactly like the Anthropic key, so
 * `has_voyage_key` is the whole truth.
 */
function VoyageKey({ settings, uid }: { settings: AppSettings; uid: string }) {
  const queryClient = useQueryClient()
  const [draftKey, setDraftKey] = useState('')

  const save = useMutation({
    mutationFn: (key: string) => updateSettings({ voyage_api_key: key }),
    onSuccess: async () => {
      setDraftKey('')
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: settingsQueryKey }),
        queryClient.invalidateQueries({ queryKey: modelsQueryKey }),
        // The embedder is built per request from this key, so the stats row and
        // the search mode both change the moment it lands.
        queryClient.invalidateQueries({ queryKey: kbQueryKey }),
      ])
    },
  })

  const masked = settings.voyage_api_key_masked || '…'
  const hint = settings.has_voyage_key
    ? `Currently set: ${masked}. Enter a new key to replace it.`
    : 'No Voyage key configured — the knowledge base stays a keyword index until there is one.'

  return (
    <div className="flex flex-col gap-2">
      <Field label="Voyage API key" htmlFor={`${uid}-voyage-key`} hint={hint}>
        <Input
          id={`${uid}-voyage-key`}
          type="password"
          tone="bg"
          autoComplete="off"
          spellCheck={false}
          placeholder="pa-…"
          value={draftKey}
          onChange={(event) => setDraftKey(event.target.value)}
        />
      </Field>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          loading={save.isPending}
          disabled={draftKey.trim() === ''}
          onClick={() => save.mutate(draftKey.trim())}
        >
          Save key
        </Button>
        {settings.has_voyage_key ? (
          <Button variant="danger" disabled={save.isPending} onClick={() => save.mutate('')}>
            Remove key
          </Button>
        ) : null}
        {save.isError ? (
          <span className="text-[11.5px] text-red">Could not save the key.</span>
        ) : null}
      </div>
    </div>
  )
}

/** Model, effort, prompt, caps — and what the month has cost so far. */
function Compile({
  uid,
  draft,
  settings,
  onEdit,
}: {
  uid: string
  draft: KnowledgeDraft
  settings: AppSettings
  onEdit: (patch: Partial<KnowledgeDraft>) => void
}) {
  const stored = settings.kb_compile_prompt
  // Guarded: a backend older than P2-24 does not send it, and a Reset button
  // that emptied the prompt would be worse than one that is disabled.
  const shipped = settings.kb_compile_prompt_default || stored

  return (
    <div className="flex flex-col gap-3 border-t border-line pt-3">
      <div className={FIELD_GRID}>
        <Field
          label="Compile mode"
          htmlFor={`${uid}-kb-compile-mode`}
          hint="In auto mode a capture compiles the entry it just made, inside the request that caused it: starring an item and saving a URL wait for the model before they answer. The only brake on auto is the monthly token budget below."
        >
          <Select
            id={`${uid}-kb-compile-mode`}
            tone="bg"
            value={draft.kb_compile_mode}
            onChange={(event) => onEdit({ kb_compile_mode: event.target.value as CompileMode })}
          >
            {COMPILE_MODES.map((mode) => (
              <option key={mode} value={mode}>
                {mode}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="Compile model" htmlFor={`${uid}-kb-compile-model`}>
          <Input
            id={`${uid}-kb-compile-model`}
            tone="bg"
            spellCheck={false}
            value={draft.kb_compile_model}
            onChange={(event) => onEdit({ kb_compile_model: event.target.value })}
          />
        </Field>

        <Field label="Compile effort" htmlFor={`${uid}-kb-compile-effort`}>
          <Select
            id={`${uid}-kb-compile-effort`}
            tone="bg"
            value={draft.kb_compile_effort}
            onChange={(event) => onEdit({ kb_compile_effort: event.target.value as Effort })}
          >
            {EFFORTS.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </Select>
        </Field>
      </div>

      <Field
        label="Compile prompt"
        htmlFor={`${uid}-kb-compile-prompt`}
        hint="The instructions every compile is given. “Reset to default” puts back the prompt this build ships with; “Revert changes” puts back the one you last saved."
      >
        <Textarea
          id={`${uid}-kb-compile-prompt`}
          tone="bg"
          mono
          rows={7}
          value={draft.kb_compile_prompt}
          onChange={(event) => onEdit({ kb_compile_prompt: event.target.value })}
        />
      </Field>
      <div className="flex flex-wrap items-center gap-2">
        {/* Two different things, and the button used to claim to be both: the
            shipped prompt is `kb_compile_prompt_default` and arrives read-only
            on every GET, because it is nowhere else the client can reach. */}
        <Button
          size="sm"
          disabled={draft.kb_compile_prompt === shipped}
          onClick={() => onEdit({ kb_compile_prompt: shipped })}
        >
          Reset to default
        </Button>
        <Button
          size="sm"
          variant="ghost"
          disabled={draft.kb_compile_prompt === stored}
          onClick={() => onEdit({ kb_compile_prompt: stored })}
        >
          Revert changes
        </Button>
        {draft.kb_compile_prompt.trim() === '' ? (
          <span className="text-[11.5px] text-red">
            The prompt cannot be empty — the compile would go out with no instructions, so the API
            refuses it too.
          </span>
        ) : null}
      </div>

      <div className={FIELD_GRID}>
        <NumberField
          id={`${uid}-kb-compile-max-chars`}
          label="Characters sent per entry"
          hint="How much of a captured article the model is shown."
          value={draft.kb_compile_max_chars}
          min={1_000}
          max={200_000}
          onChange={(value) => onEdit({ kb_compile_max_chars: value })}
        />
        <NumberField
          id={`${uid}-kb-compile-budget`}
          label="Monthly compile token budget"
          value={draft.kb_compile_monthly_token_budget}
          min={0}
          max={1_000_000_000}
          onChange={(value) => onEdit({ kb_compile_monthly_token_budget: value })}
        />
      </div>
      <p className={FIELD_HINT}>
        <strong className="font-medium text-muted">
          Counts compile tokens only — this does not include chat spend.
        </strong>{' '}
        Every compile&rsquo;s input tokens (including cached ones) and output tokens are added up for
        the calendar month, in UTC, from the knowledge base&rsquo;s own activity trail. Research chat
        is counted separately, per session — the counts are on{' '}
        <Link to="/chat" className="underline underline-offset-2 hover:text-ink">
          each chat session
        </Link>
        . At the limit, compiling stops and capture carries on.
      </p>

      <Checkbox
        id={`${uid}-kb-auto-accept`}
        label="Apply suggested topics and tags"
        hint="A compile’s topics and tags are applied straight away and stay marked “suggested”, so they are reviewable without being a daily chore. Off, they are only reported."
        checked={draft.kb_auto_accept_suggestions}
        onChange={(checked) => onEdit({ kb_auto_accept_suggestions: checked })}
      />

      <Budget />
    </div>
  )
}

/** Month-to-date spend. Two counters, never added together. */
function Budget() {
  const budget = useQuery({ queryKey: kbBudgetKey, queryFn: getBudget })

  if (budget.isPending) {
    return <p className={FIELD_HINT}>Reading this month&rsquo;s spend…</p>
  }
  if (budget.isError || !budget.data) {
    return <p className="text-[11.5px] text-red">This month&rsquo;s spend could not be read.</p>
  }

  // Read field by field below: this is a payload from a backend that need not
  // be this bundle's version, and one missing key used to take the SPA down.
  const data = budget.data
  const label = budgetLabel(data)

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <span className="text-[11.5px] text-faint">Compile spend · {data.month ?? 'this month'}</span>
        <span className={cx('text-[11.5px]', label.exhausted ? 'text-red' : 'text-muted')}>
          {label.used} of {label.limit} tokens
          {label.exhausted ? ' · spent' : ` · ${formatTokens(data.remaining ?? 0)} left`}
        </span>
      </div>
      {/* The bar is a plain div: a `progress` element cannot be themed to match
          the rest of this page without fighting four vendor pseudo-elements. */}
      <div className="h-[6px] w-full overflow-hidden rounded-full bg-panel2">
        <div
          className={cx('h-full rounded-full', label.exhausted ? 'bg-red' : 'bg-accent')}
          style={{ width: `${label.pct}%` }}
        />
      </div>
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <span className="text-[11.5px] text-faint">
          Voyage embedding tokens {data.voyage_estimated === false ? '' : '(estimated)'}
        </span>
        <span className="text-[11.5px] text-muted">{formatTokens(data.voyage ?? 0)}</span>
      </div>
      <p className={FIELD_HINT}>
        <strong className="font-medium text-muted">
          Embedding tokens, counted separately and never added to the compile budget.
        </strong>{' '}
        This is our own estimate of what was sent to Voyage (about one token per 3.6 characters), not
        a figure Voyage billed.
      </p>
    </div>
  )
}

/** The priors the two search legs are weighed with. */
function Retrieval({
  uid,
  draft,
  onEdit,
}: {
  uid: string
  draft: KnowledgeDraft
  onEdit: (patch: Partial<KnowledgeDraft>) => void
}) {
  return (
    <div className="flex flex-col gap-3 border-t border-line pt-3">
      <Checkbox
        id={`${uid}-kb-reviewed-only`}
        label="Only give reviewed entries to the model"
        hint="Narrows what the chat tools and note generation may read. Model-written entries are held back until reviewed either way."
        checked={draft.kb_reviewed_only}
        onChange={(checked) => onEdit({ kb_reviewed_only: checked })}
      />
      <Checkbox
        id={`${uid}-kb-recency-boost`}
        label="Prefer recent entries"
        hint="A small tilt towards newer captures when two hits score alike."
        checked={draft.kb_recency_boost}
        onChange={(checked) => onEdit({ kb_recency_boost: checked })}
      />
      <Checkbox
        id={`${uid}-kb-rerank`}
        label="Rerank results (arrives with reranking)"
        hint="Stored, and read by nothing yet — the reranker is a later phase. Leaving it on changes no result today."
        checked={draft.kb_rerank}
        onChange={(checked) => onEdit({ kb_rerank: checked })}
      />
      <Field
        label="Duplicate threshold"
        htmlFor={`${uid}-kb-duplicate-threshold`}
        hint="How alike two entries must be before the newer one is flagged as a possible duplicate. With no Voyage key the check is the title alone, which flags more than it should."
        className="max-w-[200px]"
      >
        <Input
          id={`${uid}-kb-duplicate-threshold`}
          type="number"
          tone="bg"
          min={0}
          max={1}
          step={0.01}
          value={draft.kb_duplicate_threshold}
          onChange={(event) => {
            const parsed = Number.parseFloat(event.target.value)
            onEdit({
              kb_duplicate_threshold: Number.isNaN(parsed)
                ? 0
                : Math.min(1, Math.max(0, parsed)),
            })
          }}
        />
      </Field>
    </div>
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
  schema: AppSettings['kb_schema_version'] | undefined
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
  const pending = stats.pending_chunks ?? 0
  const rows: [string, string][] = [
    ['Entries', `${stats.entries.toLocaleString()}${stats.deleted > 0 ? ` (${stats.deleted} deleted)` : ''}`],
    [
      'Chunks',
      pending > 0
        ? `${stats.chunks.toLocaleString()} · ${pending.toLocaleString()} waiting for an embedding`
        : stats.chunks.toLocaleString(),
    ],
    ['Entities', stats.entities.toLocaleString()],
    ['Topics', stats.topics.toLocaleString()],
    ['Keyword index (FTS5)', index?.fts5 ? 'available' : 'missing'],
    // The vector extension is optional: without it the KB is a keyword index,
    // which is a working knowledge base and not a broken one.
    ['Vector extension', vecVersionLabel(index?.vec_version)],
    ['Embeddings', stats.embeddings_configured ? 'configured' : 'not configured yet'],
  ]
  if (schema) {
    rows.push(['Schema', `v${schema.version} · ${schema.vec_dimensions} dims · ${schema.tokenizer}`])
  }

  return (
    <div className="flex flex-col gap-1.5 border-t border-line pt-3">
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
          full rebuild arrives in a later phase.
        </p>
      ) : null}
      <EmbedNow pending={pending} configured={stats.embeddings_configured === true} />
    </div>
  )
}

/**
 * "Embed now" — the Phase 2 subset of a re-index.
 *
 * `POST /kb/embed-pending` takes a bounded slice per call and reports the whole
 * backlog, so this calls it again while anything is left and the user has not
 * stopped. There is no poller anywhere in this app and this is not one: it is a
 * loop the user started and can end. `embedNow.ts::embedAgain` is the decision
 * that ends it, tested on its own — including the case that used to spin.
 *
 * There is deliberately **no "am I still mounted" ref** either. One was here and
 * it jammed the button: a ref set `true` at its declaration is never set again,
 * StrictMode's mount cleanup put it to `false` before the first click, and the
 * loop then returned after one batch without ever clearing `running`. A
 * `setState` after unmount is a silent no-op in React 18+; `stop`, which *is*
 * re-set on every run, is the only flag worth keeping.
 */
function EmbedNow({ pending, configured }: { pending: number; configured: boolean }) {
  const queryClient = useQueryClient()
  const [running, setRunning] = useState(false)
  const [embedded, setEmbedded] = useState(0)
  const [left, setLeft] = useState<number | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const stop = useRef(false)

  // Leaving the page ends the loop; the request in flight still resolves, and
  // the state it lands on belongs to a component nobody is looking at.
  useEffect(() => () => void (stop.current = true), [])

  const run = async () => {
    stop.current = false
    setRunning(true)
    setProblem(null)
    setEmbedded(0)
    let total = 0
    try {
      for (;;) {
        const result = await embedPending()
        total += result.embedded ?? 0
        setEmbedded(total)
        setLeft(result.pending ?? 0)
        if (!embedAgain(result, stop.current)) {
          break
        }
      }
    } catch (error) {
      // 409 (no key) and 502 (Voyage refused) both arrive here with a sentence
      // worth showing; the count on screen stays true either way, because the
      // chunks it did not reach are still pending.
      setProblem(embedProblem(error))
    } finally {
      setRunning(false)
      await queryClient.invalidateQueries({ queryKey: kbQueryKey })
      // `left` is this run's own count, and the run is over: hand the line back
      // to `pending`, which the refetch above has just made current. Left set,
      // it froze the count and pinned the button disabled for the life of the
      // mounted section — a star in the other pane would add pending chunks the
      // row went on denying.
      setLeft(null)
    }
  }

  // During a run, what the last call reported; otherwise the fresh stats.
  const waiting = left ?? pending

  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-[11.5px] text-faint">
        {waiting > 0
          ? `${waiting.toLocaleString()} chunks are waiting for an embedding.`
          : 'Everything captured is embedded.'}
      </span>
      {running ? (
        <Button size="sm" onClick={() => (stop.current = true)}>
          Stop
        </Button>
      ) : (
        <Button
          size="sm"
          disabled={waiting <= 0 || !configured}
          title={configured ? undefined : 'A Voyage API key is needed to embed anything.'}
          onClick={() => void run()}
        >
          Embed now
        </Button>
      )}
      {running || embedded > 0 ? (
        <span className="text-[11.5px] text-muted">{embedded.toLocaleString()} embedded</span>
      ) : null}
      {problem ? <span className="basis-full text-[11.5px] text-red">{problem}</span> : null}
    </div>
  )
}

/** The trail, on request. Mounted closed, so its query never runs unasked. */
function ActivityLog() {
  const [open, setOpen] = useState(false)

  return (
    <div className="border-t border-line pt-3">
      <Button size="sm" variant="ghost" onClick={() => setOpen(!open)}>
        {open ? 'Hide the activity log' : 'Show the activity log'}
      </Button>
      {open ? <ActivityRows /> : null}
    </div>
  )
}

function ActivityRows() {
  const activity = useQuery({
    queryKey: kbActivityKey(KB_ACTIVITY_LOG_LIMIT),
    queryFn: () => listActivity(KB_ACTIVITY_LOG_LIMIT),
  })

  if (activity.isPending) {
    return <p className={FIELD_HINT}>Reading the trail…</p>
  }
  if (activity.isError) {
    return <p className="text-[11.5px] text-red">The activity log could not be read.</p>
  }

  const rows: KbActivity[] = activity.data ?? []
  if (rows.length === 0) {
    return <p className={FIELD_HINT}>Nothing has happened yet.</p>
  }

  return (
    <ul className="mt-2 max-h-[280px] space-y-[3px] overflow-y-auto">
      {rows.map((row) => (
        <li key={row.id} className="text-[11.5px] text-muted">
          <span className="font-mono">{row.action}</span>
          <span className="text-faint" title={formatNoteDate(row.at)}>
            {' · '}
            {formatNoteDay(row.at)}
            {row.source ? ` · ${row.source}` : ''}
            {row.input_tokens + row.output_tokens > 0
              ? ` · ${formatTokens(row.input_tokens)} in / ${formatTokens(row.output_tokens)} out`
              : ''}
          </span>
          {row.detail ? <span className="text-faint"> — {row.detail}</span> : null}
        </li>
      ))}
    </ul>
  )
}
