import { useQuery } from '@tanstack/react-query'

import { fetchHealth } from '../api/client'
import { cx } from './ui/classes'

export interface BackendStatusProps {
  /** The wide rail shows the text; the narrow one is the dot and its tooltip. */
  expanded: boolean
}

/** Live backend health at the foot of the rail, polled every 30 s. */
export default function BackendStatus({ expanded }: BackendStatusProps) {
  const { data, isPending, isError } = useQuery({
    queryKey: ['health'],
    queryFn: fetchHealth,
    refetchInterval: 30_000,
  })

  const tone = isPending ? 'bg-amber' : isError ? 'bg-red' : 'bg-green'

  const label = isPending
    ? 'checking backend…'
    : isError
      ? 'backend unreachable'
      : `backend ${data.status} · v${data.version}`

  return (
    <div className="flex items-center gap-2 px-[11px] py-1" title={label}>
      <span
        className={cx('size-[7px] shrink-0 rounded-full', tone)}
        aria-hidden="true"
      />
      <span
        className={cx(
          'truncate text-[10.5px] whitespace-nowrap text-faint',
          expanded ? '' : 'sr-only',
        )}
      >
        {label}
      </span>
    </div>
  )
}
