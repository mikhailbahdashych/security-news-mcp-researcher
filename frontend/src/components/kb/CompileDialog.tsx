import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { isNotFound } from '../../api/client'
import {
  KB_COMPILE_BATCH_MAX,
  compileBatch,
  compileOutcome,
  estimateCompile,
  formatTokens,
  kbQueryKey,
  kbTopicsKey,
  listTopics,
  type KbCompileResult,
} from '../../api/kb'
import Button from '../ui/Button'
import Dialog from '../ui/Dialog'
import { FIELD_HINT } from '../ui/classes'
import NewTopic from './NewTopic'

export interface CompileDialogProps {
  /** The entries to summarise — capped at the route's 100 by the caller. */
  entryIds: number[]
  onClose: () => void
  /** Open one of the results, routed or in place. */
  onOpen: (id: number) => void
}

/**
 * "Compile N" — what it would cost, then what it did.
 *
 * The estimate comes first and always: `POST /kb/compile?estimate=1` makes no
 * model call and bills nothing, so the price of asking is never itself a cost —
 * and a monthly budget the user cannot see coming is a budget they only meet
 * when it is spent.
 *
 * Every outcome below is an HTTP 200. A spent budget, a refusal and an
 * unreadable answer are things that happen when you ask a model to summarise
 * security writing; they are explained per entry, not thrown as errors.
 */
export default function CompileDialog({ entryIds, onClose, onOpen }: CompileDialogProps) {
  const queryClient = useQueryClient()
  const ids = entryIds.slice(0, KB_COMPILE_BATCH_MAX)

  const compile = useMutation({
    mutationFn: () => compileBatch(ids),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: kbQueryKey }),
    onError: () => queryClient.invalidateQueries({ queryKey: kbQueryKey }),
  })

  const estimate = useQuery({
    queryKey: ['kb', 'compile-estimate', ids],
    queryFn: () => estimateCompile(ids),
    // A price is worth asking for once per dialog, not once per refetch — and
    // not at all once the batch has run, or the compile's own invalidation of
    // the `kb` prefix would price a batch nobody is about to send.
    enabled: compile.data === undefined,
    staleTime: Infinity,
    retry: false,
  })

  const topics = useQuery({ queryKey: kbTopicsKey, queryFn: listTopics })

  const priced = estimate.data
  const exhausted = priced ? priced.budget_remaining <= 0 : false
  const results = compile.data ?? null
  const gone = compile.isError && isNotFound(compile.error)

  return (
    <Dialog
      title={results ? 'Compiled' : `Compile ${ids.length}`}
      description={
        results
          ? 'What each entry came back with.'
          : 'Summaries, topics, tags and entities, written by the model.'
      }
      width="lg"
      onClose={onClose}
      footer={
        results ? (
          <Button variant="primary" onClick={onClose}>
            Done
          </Button>
        ) : (
          <>
            <Button onClick={onClose} disabled={compile.isPending}>
              Cancel
            </Button>
            <Button
              variant="primary"
              loading={compile.isPending}
              disabled={estimate.isPending || exhausted}
              onClick={() => compile.mutate()}
            >
              {`Compile ${ids.length}`}
            </Button>
          </>
        )
      }
    >
      <div className="flex max-h-[60vh] flex-col gap-3 overflow-y-auto">
        {results === null ? (
          <Estimate
            isPending={estimate.isPending}
            isError={estimate.isError}
            entries={priced?.entries ?? ids.length}
            inputTokens={priced?.input_tokens ?? 0}
            remaining={priced?.budget_remaining ?? 0}
            wouldExceed={priced?.would_exceed ?? false}
            exhausted={exhausted}
          />
        ) : null}

        {gone ? (
          <p className="text-[12px] text-amber">
            Nothing was compiled: one of these entries is no longer there, and the whole batch is
            refused before any of it is sent. The list has been refreshed — try again.
          </p>
        ) : null}
        {compile.isError && !gone ? (
          <p className="text-[12px] text-red">The compile request did not get through.</p>
        ) : null}

        {results?.map((result) => (
          <Result
            key={result.entry.id}
            result={result}
            topicName={(id) => topics.data?.find((topic) => topic.id === id)?.name ?? `#${id}`}
            onOpen={onOpen}
          />
        ))}
      </div>
    </Dialog>
  )
}

interface EstimateProps {
  isPending: boolean
  isError: boolean
  entries: number
  inputTokens: number
  remaining: number
  wouldExceed: boolean
  exhausted: boolean
}

/** What it would cost, before a single token is spent. */
function Estimate({
  isPending,
  isError,
  entries,
  inputTokens,
  remaining,
  wouldExceed,
  exhausted,
}: EstimateProps) {
  if (isPending) {
    return <p className={FIELD_HINT}>Pricing the batch…</p>
  }
  if (isError) {
    return (
      <p className="text-[12px] text-red">
        The estimate could not be read, so nothing is being sent. Is the backend running?
      </p>
    )
  }

  return (
    <div className="flex flex-col gap-1.5">
      <dl className="grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-x-4 gap-y-1">
        <Row label="Entries" value={String(entries)} />
        <Row label="Input tokens" value={formatTokens(inputTokens)} />
        <Row label="Budget remaining" value={formatTokens(remaining)} />
      </dl>
      {exhausted ? (
        <p className="text-[11.5px] text-red">
          The monthly compile budget is spent, so nothing would be sent. Raise it in Settings →
          Knowledge, or wait for the next month.
        </p>
      ) : wouldExceed ? (
        <p className="text-[11.5px] text-amber">
          This batch is larger than what is left of the month. Compiling stops at the limit, and the
          entries past it come back with “the monthly compile budget is spent”.
        </p>
      ) : (
        <p className={FIELD_HINT}>
          Output tokens are not in the estimate — they are counted against the month as they are
          written.
        </p>
      )}
    </div>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-[11.5px] text-faint">{label}</dt>
      <dd className="text-[11.5px] text-muted">{value}</dd>
    </div>
  )
}

/** One entry's outcome. A refusal is explained here, not thrown. */
function Result({
  result,
  topicName,
  onOpen,
}: {
  result: KbCompileResult
  topicName: (id: number) => string
  onOpen: (id: number) => void
}) {
  const tokens =
    result.input_tokens + result.output_tokens > 0
      ? `${formatTokens(result.input_tokens)} in / ${formatTokens(result.output_tokens)} out`
      : null

  return (
    <div className="rounded-[8px] border border-line px-3 py-2.5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-[12.5px] font-medium text-ink">{result.entry.title}</span>
        <Button size="sm" variant="ghost" onClick={() => onOpen(result.entry.id)}>
          Open
        </Button>
      </div>
      <p className={result.compiled ? 'text-[11.5px] text-muted' : 'text-[11.5px] text-amber'}>
        {compileOutcome(result)}
      </p>
      <p className={FIELD_HINT}>
        {[result.model, tokens].filter(Boolean).join(' · ') || 'Nothing was spent.'}
      </p>

      {result.suggested_topic_ids.length > 0 || result.suggested_tags.length > 0 ? (
        <p className="mt-1.5 text-[11.5px] text-muted">
          {/* Applied already when auto-accept is on — and still marked as
              suggestions on the entry, which is ruling I16. */}
          Suggested:{' '}
          {[
            ...result.suggested_topic_ids.map(topicName),
            ...result.suggested_tags.map((tag) => `#${tag}`),
          ].join(', ')}
        </p>
      ) : null}

      {result.new_topic ? (
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          <span className="text-[11.5px] text-muted">
            Proposed a new topic, “{result.new_topic.name}” — nothing was created.
          </span>
          <NewTopic initialName={result.new_topic.name} />
        </div>
      ) : null}
    </div>
  )
}
