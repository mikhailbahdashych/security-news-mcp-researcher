import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { conflictDetail } from '../../api/client'
import {
  compileEntry,
  compileOutcome,
  deleteEntry,
  kbQueryKey,
  mergeEntry,
  notADuplicate,
  patchEntry,
  purgeEntries,
  refreshEntry,
  undeleteEntry,
  type KbActivity,
  type KbEntry,
} from '../../api/kb'
import Button from '../ui/Button'
import Card from '../ui/Card'
import ConfirmDialog from '../ui/ConfirmDialog'
import SectionLabel from '../ui/SectionLabel'
import { needsAttention, type AttentionRow } from './attention'

export interface NeedsAttentionProps {
  /** The entries on screen — the duplicates and the model prose are found in these. */
  entries: KbEntry[]
  /** The soft-deleted entries, newest first. */
  deleted: KbEntry[]
  /** The recent trail, for the capture and compile failures in it. */
  activity: KbActivity[]
  /** Without a key a duplicate is flagged on the title alone, and the row says so. */
  embeddingsConfigured: boolean
  /** Open an entry, routed or in place — the page owns which. */
  onOpen: (id: number) => void
}

/**
 * The "Needs attention" strip — everything the knowledge base cannot settle on
 * its own, and **nothing else**.
 *
 * `attention.ts::needsAttention` is the whole rule and is tested on its own; this
 * draws its rows and hangs one action off each. It is **not** a queue over
 * everything captured: with suggestions auto-accepted there is nothing routine
 * left to confirm, and a strip that lists every save teaches you to ignore it.
 *
 * Shown only when it has something. An empty "Needs attention" is a chore that
 * is not there.
 */
export default function NeedsAttention({
  entries,
  deleted,
  activity,
  embeddingsConfigured,
  onOpen,
}: NeedsAttentionProps) {
  const queryClient = useQueryClient()
  const [confirmPurge, setConfirmPurge] = useState(false)
  const [compiled, setCompiled] = useState<string | null>(null)
  const invalidate = () => queryClient.invalidateQueries({ queryKey: kbQueryKey })

  const undo = useMutation({
    mutationFn: (id: number) => undeleteEntry(id),
    onSuccess: invalidate,
  })
  const merge = useMutation({
    mutationFn: ({ id, into }: { id: number; into: number }) => mergeEntry(id, into),
    onSuccess: invalidate,
  })
  // The answer to a false positive, which with no Voyage key is most of them:
  // the flag goes, nothing is merged and nothing is deleted.
  const dismiss = useMutation({
    mutationFn: (id: number) => notADuplicate(id),
    onSuccess: invalidate,
  })
  const review = useMutation({
    mutationFn: (id: number) => patchEntry(id, { review_status: 'reviewed' }),
    onSuccess: invalidate,
  })
  const retry = useMutation({
    mutationFn: (id: number) => refreshEntry(id),
    onSuccess: invalidate,
  })
  // Every compile outcome is an HTTP 200 — a refusal, a spent budget and a
  // missing key are all `compiled: false` — so the sentence is shown here
  // rather than thrown, exactly as the entry page does it.
  const compile = useMutation({
    mutationFn: (id: number) => compileEntry(id),
    onSuccess: async (result) => {
      setCompiled(result.compiled ? null : compileOutcome(result))
      await invalidate()
    },
  })
  // Soft: the row moves down the strip to the bin, where Undo is waiting.
  const remove = useMutation({
    mutationFn: (id: number) => deleteEntry(id),
    onSuccess: invalidate,
  })
  const purge = useMutation({
    mutationFn: (ids: number[]) => purgeEntries(ids),
    onSuccess: async () => {
      setConfirmPurge(false)
      await invalidate()
    },
  })

  const rows = needsAttention(entries, activity, { deleted, embeddingsConfigured })
  if (rows.length === 0) {
    return null
  }

  // 409 is the one refusal worth spelling out: the URL was captured again while
  // this entry was in the bin, so restoring it would make two of the same thing.
  // The same helper the entry page's own Undo uses — the two used to disagree.
  const conflict = conflictDetail(undo.error) ?? conflictDetail(merge.error)
  const failed =
    (undo.isError && conflictDetail(undo.error) === null) ||
    (merge.isError && conflictDetail(merge.error) === null) ||
    review.isError ||
    dismiss.isError ||
    retry.isError ||
    compile.isError ||
    remove.isError

  const deletedIds = deleted.map((entry) => entry.id)

  return (
    <Card tone="panel2">
      <SectionLabel as="h2" className="text-muted">
        Needs attention
      </SectionLabel>
      <ul className="mt-2 flex flex-col gap-1.5">
        {rows.map((row) => (
          <li key={row.key} className="flex flex-wrap items-center gap-2">
            <span className="min-w-0 flex-1 truncate text-[12px] text-muted" title={row.text}>
              {row.text}
            </span>
            <Actions
              row={row}
              busy={{
                merge: merge.isPending && merge.variables?.id === row.entryId,
                dismiss: dismiss.isPending && dismiss.variables === row.entryId,
                review: review.isPending && review.variables === row.entryId,
                retry: retry.isPending && retry.variables === row.entryId,
                compile: compile.isPending && compile.variables === row.entryId,
                undo: undo.isPending && undo.variables === row.entryId,
                remove: remove.isPending && remove.variables === row.entryId,
              }}
              onOpen={onOpen}
              onMerge={(id, into) => merge.mutate({ id, into })}
              onDismiss={(id) => dismiss.mutate(id)}
              onReview={(id) => review.mutate(id)}
              onRetry={(id) => retry.mutate(id)}
              onCompile={(id) => {
                setCompiled(null)
                compile.mutate(id)
              }}
              onUndo={(id) => undo.mutate(id)}
              onDelete={(id) => remove.mutate(id)}
            />
          </li>
        ))}
      </ul>

      {deletedIds.length > 0 ? (
        <div className="mt-2.5 flex flex-wrap items-center gap-2">
          <Button size="sm" variant="ghost" onClick={() => setConfirmPurge(true)}>
            Purge {deletedIds.length} deleted…
          </Button>
          <span className="text-[11px] text-faint">
            Purging is the one thing here that cannot be undone.
          </span>
        </div>
      ) : null}

      {compiled ? <p className="mt-2 text-[11.5px] text-amber">{compiled}</p> : null}
      {conflict ? <p className="mt-2 text-[11.5px] text-red">{conflict}</p> : null}
      {failed && conflict === null ? (
        <p className="mt-2 text-[11.5px] text-red">That did not go through.</p>
      ) : null}

      {confirmPurge ? (
        <ConfirmDialog
          title="Purge deleted entries"
          confirmLabel={`Purge ${deletedIds.length}`}
          busy={purge.isPending}
          error={purge.isError ? 'They could not be purged.' : undefined}
          message={
            <>
              {deletedIds.length === 1
                ? 'This permanently removes the deleted entry'
                : `This permanently removes the ${deletedIds.length} deleted entries`}{' '}
              — the text, the snapshots and the trail. It cannot be undone.
            </>
          }
          onCancel={() => setConfirmPurge(false)}
          onConfirm={() => purge.mutate(deletedIds)}
        />
      ) : null}
    </Card>
  )
}

interface ActionsProps {
  row: AttentionRow
  busy: {
    merge: boolean
    dismiss: boolean
    review: boolean
    retry: boolean
    compile: boolean
    undo: boolean
    remove: boolean
  }
  onOpen: (id: number) => void
  onMerge: (id: number, into: number) => void
  onDismiss: (id: number) => void
  onReview: (id: number) => void
  onRetry: (id: number) => void
  onCompile: (id: number) => void
  onUndo: (id: number) => void
  onDelete: (id: number) => void
}

/** One row's actions. Each row gets what its own kind can actually be settled by. */
function Actions({
  row,
  busy,
  onOpen,
  onMerge,
  onDismiss,
  onReview,
  onRetry,
  onCompile,
  onUndo,
  onDelete,
}: ActionsProps) {
  const open =
    row.entryId !== null ? (
      <Button size="sm" variant="ghost" onClick={() => onOpen(row.entryId as number)}>
        Open
      </Button>
    ) : null

  if (row.kind === 'duplicate' && row.entryId !== null && row.mergeInto !== null) {
    return (
      <>
        {open}
        <Button
          size="sm"
          loading={busy.merge}
          // The backend keeps the **older** entry whichever id is named, so the
          // button has to say which one survives before it is pressed.
          title="Folds the two together. The older entry survives and keeps both sets of notes."
          onClick={() => onMerge(row.entryId as number, row.mergeInto as number)}
        >
          Merge
        </Button>
        <Button
          size="sm"
          variant="ghost"
          loading={busy.dismiss}
          title="Clears the flag. Nothing is merged and nothing is deleted."
          onClick={() => onDismiss(row.entryId as number)}
        >
          Dismiss
        </Button>
      </>
    )
  }

  if (row.kind === 'unreviewed' && row.entryId !== null) {
    return (
      <>
        {open}
        <Button size="sm" loading={busy.review} onClick={() => onReview(row.entryId as number)}>
          Mark reviewed
        </Button>
        <Button
          size="sm"
          variant="ghost"
          loading={busy.remove}
          onClick={() => onDelete(row.entryId as number)}
        >
          Delete
        </Button>
      </>
    )
  }

  if (row.kind === 'failure') {
    // What the failure actually was decides the button. A compile the model
    // refused, or one that ran out of budget or had no key, is not fixed by
    // fetching the article again — and on a note or a finding there is no
    // source to fetch. `attention.ts::retryAction` is that rule, tested.
    const compiling = row.retry === 'compile'
    return (
      <>
        {open}
        {row.entryId !== null ? (
          <Button
            size="sm"
            loading={compiling ? busy.compile : busy.retry}
            title={compiling ? 'Summarise this entry again' : 'Read the source again'}
            onClick={() =>
              compiling ? onCompile(row.entryId as number) : onRetry(row.entryId as number)
            }
          >
            {compiling ? 'Compile' : 'Retry'}
          </Button>
        ) : null}
      </>
    )
  }

  return (
    <Button size="sm" loading={busy.undo} onClick={() => onUndo(row.entryId as number)}>
      Undo
    </Button>
  )
}
