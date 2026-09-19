import { apiGet, apiPatch, apiPost, apiPut } from './client'

export type McpStatus = 'connected' | 'error' | 'disabled' | 'not_connected'

export interface McpServer {
  name: string
  transport: 'stdio' | 'http'
  enabled: boolean
  status: McpStatus
  tool_count: number
  error: string | null
}

/** `GET`/`PUT /api/mcp/servers`. `config` is the stored blob, for the editor. */
export interface McpServerList {
  servers: McpServer[]
  config: { mcpServers: Record<string, unknown> }
}

export interface McpTool {
  namespaced_name: string
  server: string
  name: string
  description: string
  enabled: boolean
}

export interface McpToolList {
  tools: McpTool[]
  servers: McpServer[]
  /** Every tool the model would be offered — built-ins and web tools included. */
  enabled_count: number
  warn_threshold: number
}

export const mcpServersQueryKey = ['mcp', 'servers'] as const
export const mcpToolsQueryKey = ['mcp', 'tools'] as const

export const fetchMcpServers = (): Promise<McpServerList> =>
  apiGet<McpServerList>('/mcp/servers')

/** The body is the raw Claude-Desktop blob; a 422 carries a per-server message. */
export const saveMcpServers = (config: unknown): Promise<McpServerList> =>
  apiPut<McpServerList>('/mcp/servers', config)

export const reconnectMcpServer = (name: string): Promise<McpServer> =>
  apiPost<McpServer>(`/mcp/servers/${encodeURIComponent(name)}/reconnect`)

export const fetchMcpTools = (): Promise<McpToolList> => apiGet<McpToolList>('/mcp/tools')

export const setMcpToolEnabled = (namespacedName: string, enabled: boolean): Promise<McpTool> =>
  apiPatch<McpTool>(`/mcp/tools/${encodeURIComponent(namespacedName)}`, { enabled })

export const EXAMPLE_CONFIG = `{
  "mcpServers": {
    "files": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/scratch"]
    },
    "remote": {
      "url": "https://example.com/mcp",
      "headers": { "Authorization": "Bearer ..." }
    }
  }
}`
