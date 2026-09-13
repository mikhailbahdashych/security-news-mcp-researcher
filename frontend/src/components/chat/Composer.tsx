import { useRef, useState } from 'react'

import type { FeedItem } from '../../api/inbox'
import AttachmentPicker from './AttachmentPicker'

interface ComposerProps {
  streaming: boolean
  attached: FeedItem[]
  onAttach: (item: FeedItem) => void
  onDetach: (id: number) => void
  onSend: (text: string) => void
  onStop: () => void
}

export default function Composer({
  streaming,
  attached,
  onAttach,
  onDetach,
  onSend,
  onStop,
}: ComposerProps) {
  const [text, setText] = useState('')
  const textarea = useRef<HTMLTextAreaElement>(null)

  const send = () => {
    const trimmed = text.trim()
    if (!trimmed || streaming) {
      return
    }
    setText('')
    onSend(trimmed)
    textarea.current?.focus()
  }

  return (
    <div className="space-y-2 border-t border-slate-200 bg-white px-6 py-3">
      <AttachmentPicker attached={attached} onAttach={onAttach} onDetach={onDetach} />

      <div className="flex items-end gap-2">
        <textarea
          ref={textarea}
          rows={2}
          value={text}
          disabled={streaming}
          placeholder="Ask about an advisory, a CVE, or the items you starred…"
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              send()
            }
          }}
          className="flex-1 resize-none rounded-md border border-slate-300 px-3 py-2 text-sm disabled:bg-slate-50 disabled:text-slate-400"
        />

        {streaming ? (
          <button
            type="button"
            onClick={onStop}
            className="rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:border-slate-400"
          >
            Stop
          </button>
        ) : (
          <button
            type="button"
            onClick={send}
            disabled={!text.trim()}
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-40"
          >
            Send
          </button>
        )}
      </div>
    </div>
  )
}
