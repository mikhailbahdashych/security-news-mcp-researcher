import { useRef, useState } from 'react'

import type { FeedItem } from '../../api/inbox'
import Icon from '../ui/Icon'
import { COMPOSER, cx } from '../ui/classes'
import AttachmentPicker, { AttachedChips } from './AttachmentPicker'

interface ComposerProps {
  /** `hero` is the empty state's card; `bar` is the follow-up row. */
  variant: 'hero' | 'bar'
  streaming: boolean
  attached: FeedItem[]
  onAttach: (item: FeedItem) => void
  onDetach: (id: number) => void
  onSend: (text: string) => void
  onStop: () => void
  /** `<model> · inbox first, then web`, shown in the hero footer. */
  meta?: string
  autoFocus?: boolean
}

/** The round button at the end of the composer: send, or stop while a turn runs. */
function SendButton({
  size,
  streaming,
  disabled,
  onSend,
  onStop,
}: {
  size: number
  streaming: boolean
  disabled: boolean
  onSend: () => void
  onStop: () => void
}) {
  return (
    <button
      type="button"
      onClick={streaming ? onStop : onSend}
      disabled={!streaming && disabled}
      title={streaming ? 'Stop' : 'Send'}
      aria-label={streaming ? 'Stop' : 'Send'}
      style={{ width: size, height: size }}
      className={cx(
        'flex shrink-0 items-center justify-center rounded-full bg-accent-btn text-on-accent',
        'transition-opacity duration-150 hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40',
      )}
    >
      <Icon name={streaming ? 'stop' : 'send'} size={size === 30 ? 14 : 13} />
    </button>
  )
}

export default function Composer({
  variant,
  streaming,
  attached,
  onAttach,
  onDetach,
  onSend,
  onStop,
  meta,
  autoFocus = false,
}: ComposerProps) {
  const [text, setText] = useState('')
  const area = useRef<HTMLTextAreaElement>(null)
  const line = useRef<HTMLInputElement>(null)

  const send = () => {
    const trimmed = text.trim()
    if (!trimmed || streaming) {
      return
    }
    setText('')
    onSend(trimmed)
    ;(area.current ?? line.current)?.focus()
  }

  const onKeyDown = (event: { key: string; shiftKey: boolean; preventDefault: () => void }) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      send()
    }
  }

  if (variant === 'hero') {
    return (
      <div className={cx(COMPOSER, 'px-3.5 pt-3 pb-2.5')}>
        <AttachedChips attached={attached} onDetach={onDetach} className="mb-2" />
        <textarea
          ref={area}
          rows={2}
          value={text}
          autoFocus={autoFocus}
          disabled={streaming}
          placeholder="Ask about an advisory, a CVE, or the items you starred…"
          onChange={(event) => setText(event.target.value)}
          onKeyDown={onKeyDown}
          className="w-full resize-none border-none bg-transparent text-[14px] leading-[1.5] text-ink outline-none disabled:opacity-50"
        />
        <div className="mt-1.5 flex items-center gap-2">
          <AttachmentPicker attached={attached} onAttach={onAttach} />
          {meta ? <span className="truncate text-[11px] text-faint">{meta}</span> : null}
          <div className="ml-auto flex items-center">
            <SendButton
              size={30}
              streaming={streaming}
              disabled={text.trim() === ''}
              onSend={send}
              onStop={onStop}
            />
          </div>
        </div>
      </div>
    )
  }

  return (
    <div>
      <AttachedChips attached={attached} onDetach={onDetach} className="mb-2" />
      <div className={cx(COMPOSER, 'flex items-center gap-2 py-2 pr-2 pl-3.5')}>
        <AttachmentPicker attached={attached} onAttach={onAttach} placement="up" />
        <input
          ref={line}
          value={text}
          disabled={streaming}
          placeholder={streaming ? 'Answering…' : 'Ask a follow-up…'}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={onKeyDown}
          className="min-w-0 flex-1 border-none bg-transparent text-[13px] text-ink outline-none disabled:opacity-50"
        />
        <SendButton
          size={28}
          streaming={streaming}
          disabled={text.trim() === ''}
          onSend={send}
          onStop={onStop}
        />
      </div>
    </div>
  )
}
