import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useId } from 'react'

import { mcpToolsQueryKey, setMcpToolEnabled, type McpTool } from '../../api/mcp'

/** Every tool of every reachable server, grouped by server, each with a toggle. */
export default function McpToolList({ tools }: { tools: McpTool[] }) {
  if (tools.length === 0) {
    return (
      <p className="text-[11.5px] text-faint">
        No tools yet. Tools are loaded the first time this list is opened; if a server shows an
        error above, fix it and press Reconnect.
      </p>
    )
  }

  const byServer = new Map<string, McpTool[]>()
  for (const tool of tools) {
    byServer.set(tool.server, [...(byServer.get(tool.server) ?? []), tool])
  }

  return (
    <div className="flex flex-col gap-3.5">
      {[...byServer.entries()].map(([server, serverTools]) => (
        <div key={server}>
          <p className="font-mono text-[11px] text-faint">{server}</p>
          <ul className="mt-1.5 flex flex-col gap-1.5">
            {serverTools.map((tool) => (
              <ToolRow key={tool.namespaced_name} tool={tool} />
            ))}
          </ul>
        </div>
      ))}
    </div>
  )
}

function ToolRow({ tool }: { tool: McpTool }) {
  const queryClient = useQueryClient()
  const inputId = useId()

  const toggle = useMutation({
    mutationFn: (enabled: boolean) => setMcpToolEnabled(tool.namespaced_name, enabled),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: mcpToolsQueryKey }),
  })

  return (
    <li className="flex items-start gap-2.5 rounded-[8px] border border-line bg-bg px-3 py-2">
      <input
        id={inputId}
        type="checkbox"
        checked={tool.enabled}
        disabled={toggle.isPending}
        onChange={(event) => toggle.mutate(event.target.checked)}
        className="mt-[2px] size-[15px] shrink-0 accent-[var(--accent-btn)]"
      />
      <div className="min-w-0">
        <label
          htmlFor={inputId}
          className="cursor-pointer font-mono text-[12px] font-medium text-ink"
        >
          {tool.name}
        </label>
        {tool.description ? (
          <p className="text-[11px] break-words text-muted">{tool.description}</p>
        ) : null}
        <p className="font-mono text-[10px] text-faint">{tool.namespaced_name}</p>
      </div>
    </li>
  )
}
