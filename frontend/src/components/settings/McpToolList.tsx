import { useMutation, useQueryClient } from '@tanstack/react-query'

import { mcpToolsQueryKey, setMcpToolEnabled, type McpTool } from '../../api/mcp'

/** Every tool of every reachable server, grouped by server, each with a toggle. */
export default function McpToolList({ tools }: { tools: McpTool[] }) {
  if (tools.length === 0) {
    return (
      <p className="text-xs text-slate-500">
        No tools yet. Tools are loaded the first time this list is opened; if a server shows
        an error above, fix it and press Reconnect.
      </p>
    )
  }

  const byServer = new Map<string, McpTool[]>()
  for (const tool of tools) {
    byServer.set(tool.server, [...(byServer.get(tool.server) ?? []), tool])
  }

  return (
    <div className="flex flex-col gap-4">
      {[...byServer.entries()].map(([server, serverTools]) => (
        <div key={server}>
          <p className="font-mono text-[11px] text-slate-500">{server}</p>
          <ul className="mt-1.5 flex flex-col gap-1">
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

  const toggle = useMutation({
    mutationFn: (enabled: boolean) => setMcpToolEnabled(tool.namespaced_name, enabled),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: mcpToolsQueryKey }),
  })

  return (
    <li className="flex items-start gap-3 rounded-md border border-slate-200 px-3 py-2">
      <input
        id={tool.namespaced_name}
        type="checkbox"
        checked={tool.enabled}
        disabled={toggle.isPending}
        onChange={(event) => toggle.mutate(event.target.checked)}
        className="mt-0.5 size-4 shrink-0 rounded border-slate-300 accent-slate-900"
      />
      <div className="min-w-0">
        <label
          htmlFor={tool.namespaced_name}
          className="font-mono text-xs font-medium text-slate-800"
        >
          {tool.name}
        </label>
        {tool.description ? (
          <p className="text-[11px] break-words text-slate-500">{tool.description}</p>
        ) : null}
        <p className="font-mono text-[10px] text-slate-400">{tool.namespaced_name}</p>
      </div>
    </li>
  )
}
