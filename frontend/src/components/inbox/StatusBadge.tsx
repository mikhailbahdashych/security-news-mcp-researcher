import type { ItemStatus } from '../../api/inbox'
import Badge, { type BadgeTone } from '../ui/Badge'

const TONES: Record<ItemStatus, BadgeTone> = {
  unread: 'accent',
  starred: 'amber',
  dismissed: 'neutral',
}

/**
 * The item's triage state, spelled out.
 *
 * Rows use the 7px dot instead — this is for the places where the state has to
 * be named rather than coloured, like the empty list's explanation.
 */
export default function StatusBadge({ status }: { status: ItemStatus }) {
  return <Badge tone={TONES[status]}>{status}</Badge>
}
