import Button from '../ui/Button'
import Icon from '../ui/Icon'

/**
 * A turn the backend did not finish, because the backend went away.
 *
 * A turn is a task in the server process, so a restart — a `--reload` picking up
 * an edit, a Ctrl-C — ends it wherever it had got to. The runner persists as
 * it goes, so the answer is not lost, it is only cut short; the row is marked
 * `interrupted` on the next startup and this is how the user hears about it.
 *
 * Same muted card as `TurnError`: nothing broke, and a red panel under the
 * user's own question reads as breakage.
 */
export default function InterruptedNotice({ onResend }: { onResend: () => void }) {
  return (
    <div className="rounded-[12px] border border-line bg-panel px-4 py-3.5">
      <div className="flex items-center gap-2">
        <Icon name="close" size={14} className="text-faint" />
        <p className="m-0 text-[12.5px] font-semibold text-ink">This research was interrupted</p>
      </div>
      <p className="mt-1.5 text-[12px] leading-[1.6] text-muted">
        The backend restarted before the turn finished. Anything already written is kept.
      </p>
      <div className="mt-2.5">
        <Button size="sm" variant="primary" onClick={onResend}>
          Send again
        </Button>
      </div>
    </div>
  )
}
