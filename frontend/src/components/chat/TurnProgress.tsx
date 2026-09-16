import useElapsed from '../../lib/useElapsed'
import Icon from '../ui/Icon'
import { activityLabel, formatElapsed, type ActiveTool, type LiveActivity } from './liveTurn'

export interface TurnProgressProps {
  activity: LiveActivity
  activeTool: ActiveTool | null
  /** `Date.now()` when the turn started, for the counter. */
  startedAt: number
}

/**
 * What the turn is doing, while it is doing it.
 *
 * A settled tool row looks exactly the same whether the model answered a second
 * later or is twenty seconds into a silent block of reasoning, so with only
 * ticks on screen a long turn read as a hung page. This line names the phase and
 * counts, which is the whole difference between "working" and "stuck".
 *
 * Only the label is announced: the counter changes every second, and a screen
 * reader repeating it is worse than not having it.
 */
export default function TurnProgress({ activity, activeTool, startedAt }: TurnProgressProps) {
  const elapsed = useElapsed(startedAt)
  return (
    <p className="m-0 flex items-center gap-2 text-[12px] text-muted">
      <Icon name="spinner" size={13} className="shrink-0 text-faint" />
      <span aria-live="polite">{activityLabel(activity, activeTool)}</span>
      <span className="text-faint tabular-nums">{formatElapsed(elapsed)}</span>
    </p>
  )
}
