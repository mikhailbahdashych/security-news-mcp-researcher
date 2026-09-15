import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useId, useState } from 'react'

import { ApiError } from '../../api/client'
import {
  EXAMPLE_CONFIG,
  fetchMcpServers,
  fetchMcpTools,
  mcpServersQueryKey,
  mcpToolsQueryKey,
  saveMcpServers,
} from '../../api/mcp'
import Button from '../ui/Button'
import Icon from '../ui/Icon'
import Textarea from '../ui/Textarea'
import McpServerList from './McpServerList'
import McpToolList from './McpToolList'
import SettingsSection from './SettingsSection'

/** The underlined "Show tools" affordance — a link in everything but the markup. */
const LINK_BUTTON =
  'text-[12px] font-medium text-muted underline underline-offset-2 ' +
  'transition-colors duration-150 hover:text-ink'

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
        <p className="text-[11.5px] text-red">
          Could not load MCP servers. Is the backend running?
        </p>
      ) : (
        <McpServerList servers={servers} />
      )}

      <div>
        <button type="button" onClick={() => setShowTools((value) => !value)} className={LINK_BUTTON}>
          {showTools ? 'Hide tools' : 'Show tools'}
        </button>
        {showTools ? (
          <p className="mt-1 text-[11px] text-faint">
            Opening this connects to each enabled server; the first run of an npx server can take
            a while.
          </p>
        ) : null}
      </div>

      {showTools ? (
        <div className="flex flex-col gap-2.5">
          {toolsQuery.isPending ? (
            <p className="text-[11.5px] text-faint">Connecting to servers…</p>
          ) : toolsQuery.isError ? (
            <p className="text-[11.5px] text-red">Could not load the tool list.</p>
          ) : (
            <>
              {tools && tools.enabled_count > tools.warn_threshold ? (
                <p className="flex items-start gap-2 rounded-[8px] border border-line bg-panel2 px-3 py-2 text-[11.5px] text-amber">
                  <Icon name="warning" size={14} className="mt-[1px] shrink-0" />
                  <span>
                    <strong className="font-semibold">{tools.enabled_count} tools enabled</strong>{' '}
                    (built-in, web and MCP together). Past about {tools.warn_threshold} the model
                    starts choosing tools poorly, and the tool definitions alone cost prompt tokens
                    on every turn. Turn off the ones you do not use.
                  </span>
                </p>
              ) : null}
              <p className="text-[11px] text-faint">
                {tools?.enabled_count} of {tools?.warn_threshold} suggested tools enabled in total.
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
  const editorId = useId()

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
    <div className="flex flex-col gap-2.5">
      <Textarea
        id={editorId}
        tone="bg"
        mono
        rows={8}
        spellCheck={false}
        value={value}
        placeholder={EXAMPLE_CONFIG}
        onChange={(event) => {
          setDraft(event.target.value)
          setParseError(null)
          save.reset()
        }}
      />

      {parseError ? (
        <p className="font-mono text-[11px] text-red">Invalid JSON: {parseError}</p>
      ) : null}
      {serverError ? <p className="text-[11.5px] text-red">{serverError}</p> : null}
      {save.isError && !serverError ? (
        <p className="text-[11.5px] text-red">Could not save. Is the backend running?</p>
      ) : null}

      <div className="flex flex-wrap items-center gap-3">
        <Button variant="primary" loading={save.isPending} onClick={submit}>
          {save.isPending ? 'Saving…' : 'Save MCP config'}
        </Button>
        {draft !== null ? (
          <button
            type="button"
            onClick={() => {
              setDraft(null)
              setParseError(null)
              save.reset()
            }}
            className={LINK_BUTTON}
          >
            Revert
          </button>
        ) : null}
        {save.isSuccess ? <span className="text-[11.5px] text-green">Saved</span> : null}
      </div>
    </div>
  )
}
