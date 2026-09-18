import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'

import {
  KB_BULK_MAX_ITEMS,
  bulkCaptureUrl,
  cancelBulkCapture,
  kbQueryKey,
} from '../../api/kb'
import { generationId as newJobId } from '../../lib/ids'
import { SSEHttpError, streamSSE } from '../../lib/sse'
import {
  bulkDelivered,
  bulkFrame,
  bulkSummary,
  startBulk,
  type BulkState,
} from '../kb/bulkSave'
import Button from '../ui/Button'
import Dialog from '../ui/Dialog'
import Icon from '../ui/Icon'
import { FIELD_HINT } from '../ui/classes'

export interface SaveToKnowledgeProps {
  /** The Inbox selection. Trimmed to the route's 200 before it is sent. */
  itemIds: number[]
  onClose: () => void
}

/**
 * "Save to knowledge base" over the Inbox selection.
 *
 * The job outlives no page — there is no replay endpoint for it — so this panel
 * is the whole of it: it starts the run, shows one frame per finished item and
 * ends on the terminal `done`, which says what is in the database *including
 * after a Cancel*. `bulkSave.ts` is the state machine and is tested on its own.
 *
 * Three things this must not get wrong, all of them silent:
 * **the bar is keyed on the server's `total`**, because `item_ids` is
 * de-duplicated server-side; **the stream is never aborted once `done` has
 * arrived**, the lesson `components/notes/generationPhase.ts` records; and
 * **nothing sets state after the panel is gone**.
 */
export default function SaveToKnowledge({ itemIds, onClose }: SaveToKnowledgeProps) {
  const queryClient = useQueryClient()
  const ids = itemIds.slice(0, KB_BULK_MAX_ITEMS)
  const [state, setState] = useState<BulkState | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const [stopping, setStopping] = useState(false)

  // Refs, because the reader's closure outlives the render that made it: the
  // frames arrive into this, and the two decisions afterwards (may I abort? may
  // I still set state?) are read from it.
  const latest = useRef<BulkState | null>(null)
  const abort = useRef<AbortController | null>(null)
  const alive = useRef(true)

  /**
   * Stop is the **cancel alone**, and deliberately not an abort.
   *
   * The job answers a cancel with its terminal `done`, which is the only thing
   * that can say what was saved before you stopped — aborting the reader throws
   * that ending away, exactly as it would on a chat turn (`ChatPage`'s Stop).
   */
  const stop = useCallback(() => {
    if (latest.current === null || bulkDelivered(latest.current)) {
      return
    }
    setStopping(true)
    void cancelBulkCapture(latest.current.jobId).catch(() => undefined)
  }, [])

  useEffect(
    () => () => {
      // Leaving is different: there is no way back to this stream, so the reader
      // is released — and the run is cancelled, because a disconnected bulk job
      // is one nobody can see, stop or resume.
      alive.current = false
      if (latest.current && !bulkDelivered(latest.current)) {
        void cancelBulkCapture(latest.current.jobId).catch(() => undefined)
        abort.current?.abort()
      }
    },
    [],
  )

  const start = useCallback(async () => {
    const jobId = newJobId()
    const controller = new AbortController()
    abort.current = controller
    latest.current = startBulk(jobId)
    setState(latest.current)
    setFailure(null)
    setStopping(false)

    try {
      await streamSSE({
        url: bulkCaptureUrl(),
        // The client mints the id: Cancel has to reach the job before the first
        // frame has told us what the server would have called it.
        body: { item_ids: ids, job_id: jobId },
        signal: controller.signal,
        onEvent: ({ event, data }) => {
          const next = bulkFrame(latest.current ?? startBulk(jobId), event, data)
          latest.current = next
          if (alive.current) {
            setState(next)
          }
        },
      })
    } catch (error) {
      if (alive.current && !(latest.current && bulkDelivered(latest.current))) {
        setFailure(
          controller.signal.aborted
            ? 'Stopped. Everything saved before that is saved.'
            : error instanceof SSEHttpError
              ? error.message
              : 'The save could not be started. Is the backend running?',
        )
      }
    } finally {
      if (abort.current === controller) {
        abort.current = null
      }
      // Duplicate flags arrive only in `done`, and the entries themselves are
      // new — both lists have to be re-read, whichever way the run ended.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: kbQueryKey }),
        queryClient.invalidateQueries({ queryKey: ['items'] }),
      ])
    }
  }, [ids, queryClient])

  const running = state !== null && state.phase === 'running' && failure === null
  const finished = state !== null && bulkDelivered(state)
  const dropped = itemIds.length - ids.length

  return (
    <Dialog
      title="Save to knowledge base"
      description={`${ids.length} selected item${ids.length === 1 ? '' : 's'}.`}
      // Closing is leaving, and the unmount cancels the run and releases the
      // reader — so there is nothing extra to do here.
      onClose={onClose}
      footer={
        finished ? (
          <Button variant="primary" onClick={onClose}>
            Done
          </Button>
        ) : running ? (
          <Button disabled={stopping} onClick={stop}>
            {stopping ? 'Stopping…' : 'Stop'}
          </Button>
        ) : (
          <>
            <Button onClick={onClose}>Cancel</Button>
            <Button variant="primary" onClick={() => void start()}>
              Save {ids.length}
            </Button>
          </>
        )
      }
    >
      <div className="flex flex-col gap-2.5">
        {state === null ? (
          <p className="text-[12px] text-muted">
            Each item is fetched and its article extracted, eight at a time, so a long selection is
            minutes of work. Stopping keeps everything saved up to that point.
          </p>
        ) : null}
        {dropped > 0 ? (
          <p className="text-[11.5px] text-amber">
            One save takes {KB_BULK_MAX_ITEMS} items at most, so the last {dropped} are not in this
            run.
          </p>
        ) : null}

        {state !== null ? <Progress state={state} running={running} /> : null}

        {finished ? (
          <>
            <p className="text-[12px] text-muted">{bulkSummary(state)}</p>
            {state.duplicates > 0 ? (
              <p className={FIELD_HINT}>
                The possible duplicates are listed under “Needs attention” on the Knowledge page,
                with a Merge for each.
              </p>
            ) : null}
            {state.error?.type === 'cancelled' ? (
              <p className={FIELD_HINT}>
                A stopped run leaves its chunks unembedded: those entries are keyword-searchable now,
                and Settings → Knowledge → Embed now finishes them.
              </p>
            ) : null}
          </>
        ) : null}

        {state?.lastSkip && !finished ? (
          <p className={FIELD_HINT}>Last skipped: {state.lastSkip}</p>
        ) : null}
        {state?.error && !finished ? (
          <p className="text-[12px] text-amber">{state.error.message}</p>
        ) : null}
        {failure ? <p className="text-[12px] text-red">{failure}</p> : null}
      </div>
    </Dialog>
  )
}

/** The bar, keyed on the job's own `total` and never on the ids that were sent. */
function Progress({ state, running }: { state: BulkState; running: boolean }) {
  const pct = state.total > 0 ? Math.min(100, Math.round((state.done / state.total) * 100)) : 0

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between gap-3 text-[11.5px] text-muted">
        <span className="flex items-center gap-1.5">
          {running ? <Icon name="spinner" size={13} /> : null}
          {state.total > 0 ? `${state.done} of ${state.total}` : 'Starting…'}
        </span>
        <span className="text-faint">{state.total > 0 ? `${pct}%` : ''}</span>
      </div>
      <div className="h-[6px] w-full overflow-hidden rounded-full bg-panel2">
        <div className="h-full rounded-full bg-accent" style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}
