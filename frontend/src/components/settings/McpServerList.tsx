import { useMutation, useQueryClient } from '@tanstack/react-query'

import {
  mcpServersQueryKey,
  mcpToolsQueryKey,
  reconnectMcpServer,
  type McpServer,
  type McpStatus,
} from '../../api/mcp'
import Badge, { type BadgeTone } from '../ui/Badge'
import Button from '../ui/Button'

const STATUS_STYLE: Record<McpStatus, { label: string; tone: BadgeTone }> = {
  connected: { label: 'connected', tone: 'accent' },
  error: { label: 'error', tone: 'red' },
  disabled: { label: 'disabled', tone: 'neutral' },
  not_connected: { label: 'not connected', tone: 'neutral' },
}

/** Configured servers with their live status and a per-server reconnect. */
export default function McpServerList({ servers }: { servers: McpServer[] }) {
  if (servers.length === 0) {
    return (
      <p className="text-[11.5px] text-faint">
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
    <li className="rounded-[8px] border border-line bg-bg px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[12px] font-medium text-ink">{server.name}</span>
        <Badge>{server.transport}</Badge>
        <Badge tone={status.tone}>{status.label}</Badge>
        {server.status === 'connected' ? (
          <span className="text-[11px] text-faint">
            {server.tool_count} {server.tool_count === 1 ? 'tool' : 'tools'}
          </span>
        ) : null}

        <Button
          size="sm"
          className="ml-auto"
          loading={reconnect.isPending}
          onClick={() => reconnect.mutate()}
        >
          {reconnect.isPending ? 'Connecting…' : 'Reconnect'}
        </Button>
      </div>

      {server.error ? (
        <p className="mt-1.5 font-mono text-[11px] break-words text-red">{server.error}</p>
      ) : null}
      {reconnect.isError ? (
        <p className="mt-1.5 text-[11px] text-red">Could not reach the backend.</p>
      ) : null}
    </li>
  )
}
