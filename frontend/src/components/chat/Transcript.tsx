import {
  blocksToText,
  blocksToThinking,
  errorFromStopReason,
  type ChatMessage,
  type ToolCallRow,
} from '../../api/chat'
import Markdown from './Markdown'
import ThinkingPane from './ThinkingPane'
import ToolCallCard, { type ToolCardState } from './ToolCallCard'
import TurnError from './TurnError'

function cardFromRow(row: ToolCallRow): ToolCardState {
  const result = row.result_json as { content?: string } | null
  return {
    toolUseId: row.tool_use_id ?? String(row.id),
    name: row.name ?? 'tool',
    source: row.source ?? 'builtin',
    partialJson: JSON.stringify(row.input_json ?? {}),
    input: (row.input_json as Record<string, unknown>) ?? undefined,
    status: row.is_error ? 'error' : 'ok',
    preview: typeof result?.content === 'string' ? result.content.slice(0, 600) : undefined,
    durationMs: row.duration_ms ?? undefined,
  }
}

/** The persisted part of the conversation, rendered from `content_json`. */
export default function Transcript({ messages }: { messages: ChatMessage[] }) {
  return (
    <div className="space-y-4">
      {messages.map((message) => {
        // Tool results are shown on the assistant turn that asked for them, not
        // as messages of their own.
        if (message.kind === 'tool_result') {
          return null
        }

        if (message.kind === 'user') {
          return (
            <div key={message.id} className="flex justify-end">
              <div className="max-w-2xl whitespace-pre-wrap rounded-lg bg-slate-900 px-3 py-2 text-sm text-white">
                {blocksToText(message.content_json)}
              </div>
            </div>
          )
        }

        const thinking = blocksToThinking(message.content_json)
        const text = blocksToText(message.content_json)
        // A refusal stores an empty content list; without this the turn would
        // render as nothing at all, both now and after every future reload.
        const terminal = errorFromStopReason(message)
        return (
          <div key={message.id} className="space-y-2">
            {thinking ? <ThinkingPane text={thinking} streaming={false} /> : null}
            {message.tool_calls.map((row) => (
              <ToolCallCard key={row.id} card={cardFromRow(row)} />
            ))}
            {text ? <Markdown>{text}</Markdown> : null}
            {terminal ? <TurnError error={terminal} /> : null}
          </div>
        )
      })}
    </div>
  )
}
