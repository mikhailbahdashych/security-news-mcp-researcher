import { useMemo } from 'react'

import { collectSources, type ErrorPayload, type TurnAttachment, type TurnStep } from '../../api/chat'
import Icon from '../ui/Icon'
import { cx } from '../ui/classes'
import Markdown from './Markdown'
import SourcesGrid from './SourcesGrid'
import StepsCard from './StepsCard'
import TurnError from './TurnError'

export interface AnswerTurnProps {
  question: string
  attachments: TurnAttachment[]
  steps: TurnStep[]
  answer: string
  error: ErrorPayload | null
  /** Follow-ups sit under a rule, with a smaller heading. */
  followUp: boolean
  /** The turn currently on the wire. */
  streaming?: boolean
}

function AttachedChip({ attachment }: { attachment: TurnAttachment }) {
  const label = (
    <>
      <Icon name="attach" size={11} className="shrink-0 text-faint" />
      <span className="truncate">{attachment.title}</span>
    </>
  )
  const className =
    'inline-flex max-w-[260px] items-center gap-1.5 rounded-full border border-line bg-panel ' +
    'px-2.5 py-[3px] text-[11px] text-muted no-underline'
  return attachment.url ? (
    <a
      href={attachment.url}
      target="_blank"
      rel="noreferrer noopener"
      className={cx(className, 'hover:text-ink')}
      title={attachment.title}
    >
      {label}
    </a>
  ) : (
    <span className={className} title={attachment.title}>
      {label}
    </span>
  )
}

/**
 * One question and its answer.
 *
 * The same component renders a stored turn and the one streaming right now —
 * they differ only in where the steps came from, which is the whole point of
 * deriving both through `api/chat`.
 */
export default function AnswerTurn({
  question,
  attachments,
  steps,
  answer,
  error,
  followUp,
  streaming = false,
}: AnswerTurnProps) {
  const sources = useMemo(() => collectSources(steps), [steps])
  const Heading = followUp ? 'h3' : 'h2'

  return (
    <article className={cx('flex flex-col gap-3.5', followUp && 'border-t border-line pt-4')}>
      {attachments.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {attachments.map((attachment) => (
            <AttachedChip key={attachment.id} attachment={attachment} />
          ))}
        </div>
      ) : null}

      {question ? (
        <Heading
          className={cx(
            'm-0 font-display font-medium tracking-[-0.01em] text-pretty',
            followUp ? 'text-[18px] leading-[1.35]' : 'text-[23px] leading-[1.3]',
          )}
        >
          {question}
        </Heading>
      ) : null}

      <StepsCard steps={steps} />
      <SourcesGrid sources={sources} />

      {answer ? <Markdown sources={sources}>{answer}</Markdown> : null}

      {streaming && !answer && steps.length === 0 ? (
        <p className="flex items-center gap-2 text-[12px] text-faint">
          <Icon name="spinner" size={13} />
          Working…
        </p>
      ) : null}

      {error ? <TurnError error={error} /> : null}
    </article>
  )
}
