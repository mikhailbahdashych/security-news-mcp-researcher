import { useState } from 'react'

interface ThinkingPaneProps {
  text: string
  streaming: boolean
}

/**
 * The reasoning summary, collapsed by default.
 *
 * It exists mostly to stop the UI looking frozen: with `thinking_display:
 * "summarized"` a long reasoning pause produces thinking deltas well before the
 * first text delta, so there is always something moving.
 */
export default function ThinkingPane({ text, streaming }: ThinkingPaneProps) {
  const [open, setOpen] = useState(false)
  if (!text && !streaming) {
    return null
  }

  return (
    <div className="rounded-md border border-slate-200 bg-slate-50">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-slate-600 hover:text-slate-900"
      >
        <span className={`transition-transform ${open ? 'rotate-90' : ''}`}>›</span>
        <span className="font-medium">{streaming ? 'Thinking…' : 'Thinking'}</span>
        {streaming && !open ? (
          <span className="truncate text-slate-400">{text.slice(-80)}</span>
        ) : null}
      </button>
      {open ? (
        <pre className="max-h-72 overflow-y-auto whitespace-pre-wrap px-3 pb-3 text-xs leading-relaxed text-slate-600">
          {text || '…'}
        </pre>
      ) : null}
    </div>
  )
}
