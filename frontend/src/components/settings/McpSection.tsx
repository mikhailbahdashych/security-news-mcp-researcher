import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { ApiError } from '../../api/client'
import {
  EXAMPLE_CONFIG,
  fetchMcpServers,
  fetchMcpTools,
  mcpServersQueryKey,
  mcpToolsQueryKey,
  saveMcpServers,
} from '../../api/mcp'
import McpServerList from './McpServerList'
import McpToolList from './McpToolList'
import SettingsSection from './SettingsSection'

/**
 * The MCP panel: a JSON editor, the server status board, and the per-tool toggles.
 *
 * The two queries are deliberately different in cost. `servers` is cheap and never
 * connects, so it loads with the page; `tools` is what triggers the lazy connect, so
 * it is only fetched once the user opens the tool list.
 */
export default function McpSection() {
  const queryClient = useQueryClient()
  const [showTools, setShowTools] = useState(false)

  const serversQuery = useQuery({ queryKey: mcpServersQueryKey, queryFn: fetchMcpServers })
  const toolsQuery = useQuery({
    queryKey: mcpToolsQueryKey,
    queryFn: fetchMcpTools,
    enabled: showTools,
  })

  const servers = serversQuery.data?.servers ?? []
  const tools = toolsQuery.data

  return (
    <SettingsSection
      title="MCP servers"
      description="Paste the same mcpServers JSON you would give Claude Desktop. Their tools become callable from the research chat."
    >
      <ConfigEditor
        stored={serversQuery.data?.config}
        onSaved={async () => {
          await Promise.all([
            queryClient.invalidateQueries({ queryKey: mcpServersQueryKey }),
            queryClient.invalidateQueries({ queryKey: mcpToolsQueryKey }),
          ])
        }}
      />

      {serversQuery.isError ? (
        <p className="text-xs text-rose-600">Could not load MCP servers. Is the backend running?</p>
      ) : (
        <McpServerList servers={servers} />
      )}

      {tools && tools.enabled_count > tools.warn_threshold ? (
        <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          <strong>{tools.enabled_count} tools enabled</strong> (built-in, web and MCP together).
          Past about {tools.warn_threshold} the model starts choosing tools poorly, and the tool
          definitions alone cost prompt tokens on every turn. Turn off the ones you do not use.
        </p>
      ) : null}

      <div>
        <button
          type="button"
          onClick={() => setShowTools((value) => !value)}
          className="text-xs font-medium text-slate-700 underline underline-offset-2"
        >
          {showTools ? 'Hide tools' : 'Show tools'}
        </button>
        {showTools ? (
          <p className="mt-1 text-[11px] text-slate-500">
            Opening this connects to each enabled server; the first run of an `npx` server can
            take a while.
          </p>
        ) : null}
      </div>

      {showTools ? (
        <div>
          {toolsQuery.isPending ? (
            <p className="text-xs text-slate-500">Connecting to servers…</p>
          ) : toolsQuery.isError ? (
            <p className="text-xs text-rose-600">Could not load the tool list.</p>
          ) : (
            <>
              <p className="mb-2 text-[11px] text-slate-500">
                {tools?.enabled_count} of {tools?.warn_threshold} suggested tools enabled in
                total.
              </p>
              <McpToolList tools={tools?.tools ?? []} />
            </>
          )}
        </div>
      ) : null}
    </SettingsSection>
  )
}

/** The JSON textarea. Parsed client-side first, so a typo never reaches the API. */
function ConfigEditor({
  stored,
  onSaved,
}: {
  stored: { mcpServers: Record<string, unknown> } | undefined
  onSaved: () => Promise<void>
}) {
  const [draft, setDraft] = useState<string | null>(null)
  const [parseError, setParseError] = useState<string | null>(null)

  const serialised = stored ? JSON.stringify(stored, null, 2) : ''
  const value = draft ?? serialised

  const save = useMutation({
    mutationFn: (config: unknown) => saveMcpServers(config),
    onSuccess: async () => {
      setDraft(null)
      await onSaved()
    },
  })

  const submit = () => {
    save.reset()
    setParseError(null)
    let parsed: unknown
    try {
      parsed = JSON.parse(value.trim() === '' ? '{"mcpServers": {}}' : value)
    } catch (error) {
      setParseError(error instanceof Error ? error.message : 'Invalid JSON')
      return
    }
    save.mutate(parsed)
  }

  // A 422 carries the backend's own per-server message; anything else is a plain failure.
  const serverError =
    save.error instanceof ApiError && save.error.status === 422 ? save.error.detail : null

  return (
    <div className="flex flex-col gap-2">
      <textarea
        id="mcp-config"
        rows={10}
        spellCheck={false}
        value={value}
        placeholder={EXAMPLE_CONFIG}
        onChange={(event) => {
          setDraft(event.target.value)
          setParseError(null)
          save.reset()
        }}
        className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 font-mono text-xs text-slate-900 shadow-xs outline-none focus:border-slate-500 focus:ring-2 focus:ring-slate-200"
      />

      {parseError ? (
        <p className="font-mono text-[11px] text-rose-600">Invalid JSON: {parseError}</p>
      ) : null}
      {serverError ? <p className="text-xs text-rose-600">{serverError}</p> : null}
      {save.isError && !serverError ? (
        <p className="text-xs text-rose-600">Could not save. Is the backend running?</p>
      ) : null}

      <div className="flex items-center gap-3">
        <button
          type="button"
          disabled={save.isPending}
          onClick={submit}
          className="rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-800 disabled:bg-slate-300"
        >
          {save.isPending ? 'Saving…' : 'Save MCP config'}
        </button>
        {draft !== null ? (
          <button
            type="button"
            onClick={() => {
              setDraft(null)
              setParseError(null)
              save.reset()
            }}
            className="text-xs text-slate-500 underline underline-offset-2"
          >
            Revert
          </button>
        ) : null}
        {save.isSuccess ? <span className="text-xs text-emerald-700">Saved.</span> : null}
      </div>
    </div>
  )
}
