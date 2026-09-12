import { useQuery } from '@tanstack/react-query'

import { fetchHealth } from '../api/client'

/** Demo usage of the API client + React Query: live backend health in the sidebar footer. */
export default function BackendStatus() {
  const { data, isPending, isError } = useQuery({
    queryKey: ['health'],
    queryFn: fetchHealth,
    refetchInterval: 30_000,
  })

  const tone = isPending
    ? 'bg-slate-300'
    : isError
      ? 'bg-rose-500'
      : 'bg-emerald-500'

  const label = isPending
    ? 'checking backend…'
    : isError
      ? 'backend unreachable'
      : `backend ${data.status} · v${data.version}`

  return (
    <p className="flex items-center gap-2 text-xs text-slate-500">
      <span className={`inline-block size-2 rounded-full ${tone}`} aria-hidden="true" />
      {label}
    </p>
  )
}
