import type { FeedItem } from '../../api/inbox'
import { PILL, cx } from '../ui/classes'
import Composer from './Composer'

interface EmptyResearchProps {
  /** The split view's right pane: it did not ask for the caret. */
  embedded: boolean
  attached: FeedItem[]
  onAttach: (item: FeedItem) => void
  onDetach: (id: number) => void
  onSend: (text: string) => void
  onStop: () => void
  /** `<model> · inbox first, then web`. */
  meta?: string
}

/** Openers that show what the assistant is for — a chip sends as it is written. */
const SUGGESTIONS = [
  'What was added to KEV this week?',
  'Summarize my starred items',
  "Draft Monday's meeting notes",
]

/**
 * The page before there is anything to read.
 *
 * Never on screen while a turn streams: `ChatPage`'s `empty` requires no live
 * prompt, and a streaming turn always has one. So nothing here takes a
 * `streaming` flag — the composer and the chips cannot be reached mid-turn.
 */
export default function EmptyResearch({
  embedded,
  attached,
  onAttach,
  onDetach,
  onSend,
  onStop,
  meta,
}: EmptyResearchProps) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center overflow-y-auto p-6">
      <div className="flex w-full max-w-[640px] flex-col gap-[18px]">
        <h2 className="m-0 text-center font-display text-[27px] font-medium tracking-[-0.01em] text-pretty">
          What are we researching?
        </h2>

        <Composer
          variant="hero"
          // Focusing on mount is right for the page the user navigated to and
          // wrong for a pane that merely appeared beside it — the caret would
          // jump out of whatever they were reading on the left.
          autoFocus={!embedded}
          streaming={false}
          attached={attached}
          onAttach={onAttach}
          onDetach={onDetach}
          onSend={onSend}
          onStop={onStop}
          meta={meta}
        />

        <div className="flex flex-wrap justify-center gap-2">
          {SUGGESTIONS.map((suggestion) => (
            <button
              key={suggestion}
              type="button"
              onClick={() => onSend(suggestion)}
              className={cx(
                PILL,
                'transition-colors duration-150 hover:border-faint hover:text-ink',
              )}
            >
              {suggestion}
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}
