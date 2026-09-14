import { useState } from 'react'

import { formatMs, type TurnStep } from '../../api/chat'
import Badge from '../ui/Badge'
import Icon from '../ui/Icon'
import { cx } from '../ui/classes'

/** ✓ / spinner / ✗ — how the row reports what the step did. */
function StatusGlyph({ status }: { status: TurnStep['status'] }) {
  if (status === 'running') {
    return <Icon name="spinner" size={12} className="text-faint" />
  }
  if (status === 'error') {
    return <Icon name="close" size={12} className="text-red" strokeWidth={2} />
  }
  return <Icon name="check" size={12} className="text-green" strokeWidth={2} />
}

const PRE = 'm-0 overflow-auto rounded-[8px] bg-code px-2.5 py-2 font-mono text-[11px] leading-[1.55] text-muted'

function StepRow({ step, first }: { step: TurnStep; first: boolean }) {
  const [open, setOpen] = useState(false)
  const expandable =
    step.body !== null || step.args !== null || step.preview !== null || step.links.length > 0

  return (
    <div className={first ? undefined : 'border-t border-line'}>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        disabled={!expandable}
        className={cx(
          'flex w-full items-center gap-2 px-3 py-2 text-left transition-colors duration-150',
          expandable ? 'cursor-pointer hover:bg-hover' : 'cursor-default',
        )}
      >
        <Icon
          name="chevronRight"
          size={12}
          className={cx(
            'text-faint transition-transform duration-150',
            open && 'rotate-90',
            !expandable && 'opacity-0',
          )}
        />
        <span
          className={cx(
            'shrink-0 text-[12px] font-medium',
            step.kind === 'thinking' ? 'text-muted italic' : 'font-mono text-ink',
          )}
        >
          {step.name}
        </span>
        {step.tag ? <Badge className="shrink-0">{step.tag}</Badge> : null}
        <span className="min-w-0 flex-1 truncate text-[11.5px] text-faint">{step.hint}</span>
        <StatusGlyph status={step.status} />
        {step.durationMs !== null ? (
          <span className="shrink-0 text-[10px] text-faint">{formatMs(step.durationMs)}</span>
        ) : null}
      </button>

      {open ? (
        <div className="flex flex-col gap-2 border-t border-line bg-bg px-3 py-2.5">
          {step.body ? (
            <pre className="m-0 max-h-[200px] overflow-auto font-sans text-[12px] leading-[1.6] whitespace-pre-wrap text-muted">
              {step.body}
            </pre>
          ) : null}
          {step.args ? <pre className={cx(PRE, 'max-h-[200px]')}>{step.args}</pre> : null}
          {step.links.length > 0 ? (
            <div className="flex flex-col gap-[3px]">
              {step.links.map((link, index) => (
                <span key={`${link.url ?? link.title}-${index}`} className="text-[12px]">
                  {link.url ? (
                    <a
                      href={link.url}
                      target="_blank"
                      rel="noreferrer noopener"
                      className="underline underline-offset-2"
                    >
                      {link.title || link.url}
                    </a>
                  ) : (
                    <span className="text-muted">{link.title}</span>
                  )}
                </span>
              ))}
            </div>
          ) : null}
          {step.preview ? (
            <pre className={cx(PRE, 'max-h-[160px] whitespace-pre-wrap')}>{step.preview}</pre>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

/**
 * What the assistant did before it answered.
 *
 * Every row is collapsed by default: the point of the card is that the work is
 * *auditable*, not that it is read. The hint line is what makes it skimmable.
 */
export default function StepsCard({ steps }: { steps: TurnStep[] }) {
  if (steps.length === 0) {
    return null
  }
  return (
    <div className="overflow-hidden rounded-[12px] border border-line bg-panel">
      {steps.map((step, index) => (
        <StepRow key={step.key} step={step} first={index === 0} />
      ))}
    </div>
  )
}
