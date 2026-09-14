import { useMutation, useQueryClient } from '@tanstack/react-query'

import {
  mcpServersQueryKey,
  mcpToolsQueryKey,
  reconnectMcpServer,
  type McpServer,
  type McpStatus,
} from '../../api/mcp'

const STATUS_STYLE: Record<McpStatus, { label: string; className: string }> = {
  connected: { label: 'connected', className: 'border-emerald-200 bg-emerald-50 text-emerald-700' },
  error: { label: 'error', className: 'border-rose-200 bg-rose-50 text-rose-700' },
  disabled: { label: 'disabled', className: 'border-slate-200 bg-slate-100 text-slate-500' },
  not_connected: {
    label: 'not connected',
    className: 'border-slate-200 bg-white text-slate-400',
  },
}

/** Configured servers with their live status and a per-server reconnect. */
export default function McpServerList({ servers }: { servers: McpServer[] }) {
  if (servers.length === 0) {
    return (
      <p className="text-xs text-slate-500">
        No MCP servers configured yet. Paste a config above and save.
      </p>
    )
  }

  return (
    <ul className="flex flex-col gap-2">
      {servers.map((server) => (
        <ServerRow key={server.name} server={server} />
      ))}
    </ul>
  )
}

function ServerRow({ server }: { server: McpServer }) {
  const queryClient = useQueryClient()
  const status = STATUS_STYLE[server.status] ?? STATUS_STYLE.not_connected

  const reconnect = useMutation({
    mutationFn: () => reconnectMcpServer(server.name),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: mcpServersQueryKey }),
        queryClient.invalidateQueries({ queryKey: mcpToolsQueryKey }),
      ])
    },
  })

  return (
    <li className="rounded-md border border-slate-200 bg-white px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs font-medium text-slate-800">{server.name}</span>
        <Badge className="border-slate-200 bg-slate-50 text-slate-500">{server.transport}</Badge>
        <Badge className={status.className}>{status.label}</Badge>
        {server.status === 'connected' ? (
          <span className="text-[11px] text-slate-500">
            {server.tool_count} {server.tool_count === 1 ? 'tool' : 'tools'}
          </span>
        ) : null}

        <button
          type="button"
          disabled={reconnect.isPending}
          onClick={() => reconnect.mutate()}
          className="ml-auto rounded border border-slate-300 px-2 py-1 text-[11px] text-slate-700 hover:bg-slate-50 disabled:text-slate-400"
        >
          {reconnect.isPending ? 'Connecting…' : 'Reconnect'}
        </button>
      </div>

      {server.error ? (
        <p className="mt-1.5 font-mono text-[11px] break-words text-rose-600">{server.error}</p>
      ) : null}
      {reconnect.isError ? (
        <p className="mt-1.5 text-[11px] text-rose-600">Could not reach the backend.</p>
      ) : null}
    </li>
  )
}

function Badge({ children, className }: { children: React.ReactNode; className: string }) {
  return (
    <span
      className={`rounded border px-1.5 py-0.5 text-[10px] tracking-wide uppercase ${className}`}
    >
      {children}
    </span>
  )
}
