import { useMutation, useQueryClient } from '@tanstack/react-query'

import { conflictDetail } from '../../api/client'
import { kbQueryKey, undeleteEntry, type KbEntry } from '../../api/kb'
import Button from '../ui/Button'
import Card from '../ui/Card'
import SectionLabel from '../ui/SectionLabel'

export interface NeedsAttentionProps {
  /** The soft-deleted entries, newest first. */
  deleted: KbEntry[]
}

/**
 * The "Needs attention" strip — everything the knowledge base cannot settle on
 * its own.
 *
 * In Phase 1 that is exactly one thing: an entry you deleted, so that Undo has
 * somewhere to live. Flagged duplicates, capture failures and unreviewed
 * model-authored entries join it in Phase 2, when there is anything to flag. It
 * is **not** a queue over everything captured: with suggestions auto-accepted
 * there is nothing routine left to confirm, and a strip that lists every save
 * teaches you to ignore it.
 *
 * Shown only when it has something. An empty "Needs attention" is a chore that
 * is not there.
 */
export default function NeedsAttention({ deleted }: NeedsAttentionProps) {
  const queryClient = useQueryClient()

  const undo = useMutation({
    mutationFn: (id: number) => undeleteEntry(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: kbQueryKey }),
  })

  if (deleted.length === 0) {
    return null
  }

  // 409 is the one refusal worth spelling out: the URL was captured again while
  // this entry was in the bin, so restoring it would make two of the same thing.
  // The same helper the entry page's own Undo uses — the two used to disagree.
  const conflict = conflictDetail(undo.error)

  return (
    <Card tone="panel2">
      <SectionLabel as="h2" className="text-muted">
        Needs attention
      </SectionLabel>
      <ul className="mt-2 flex flex-col gap-1.5">
        {deleted.map((entry) => (
          <li key={entry.id} className="flex items-center gap-3">
            <span className="min-w-0 flex-1 truncate text-[12px] text-muted">
              Deleted — {entry.title}
            </span>
            <Button
              size="sm"
              loading={undo.isPending && undo.variables === entry.id}
              onClick={() => undo.mutate(entry.id)}
            >
              Undo
            </Button>
          </li>
        ))}
      </ul>
      {conflict ? <p className="mt-2 text-[11.5px] text-red">{conflict}</p> : null}
      {undo.isError && conflict === null ? (
        <p className="mt-2 text-[11.5px] text-red">That entry could not be restored.</p>
      ) : null}
    </Card>
  )
}
