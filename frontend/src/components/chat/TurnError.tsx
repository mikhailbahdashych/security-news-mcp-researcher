import type { ErrorPayload } from '../../api/chat'
import Badge from '../ui/Badge'
import Icon from '../ui/Icon'
import type { IconName } from '../ui/Icon'

const COPY: Record<string, { title: string; body: string; icon: IconName }> = {
  turn_limit: {
    title: 'The assistant stopped early',
    body: 'It reached the tool-call limit for one turn. Ask a narrower question, or raise the limit in Settings.',
    icon: 'warning',
  },
  max_tokens: {
    title: 'The answer was cut off',
    body: 'The response hit the output limit before it finished.',
    icon: 'warning',
  },
  rate_limit: {
    title: 'Rate limited',
    body: 'The API asked us to slow down. Wait a moment and try again.',
    icon: 'warning',
  },
  cancelled: {
    title: 'Stopped',
    body: 'You stopped this turn. Anything already written is kept.',
    icon: 'stop',
  },
  connection: {
    title: 'Connection problem',
    body: 'The request to the API could not be completed.',
    icon: 'warning',
  },
  api_error: { title: 'The API returned an error', body: '', icon: 'warning' },
}

/**
 * A refusal is not a failure, and must not look like one.
 *
 * Opus 5 ships elevated cybersecurity safeguards and this app's whole domain is
 * security content, so benign questions occasionally trip a classifier. It gets
 * its own calm treatment — and it is never folded into the transcript as if the
 * assistant had answered.
 *
 * Every state is the same muted card: a stopped turn is an outcome, not an
 * alarm, and a red panel in the answer's position reads as breakage.
 */
export default function TurnError({ error }: { error: ErrorPayload }) {
  if (error.type === 'refusal') {
    return (
      <div className="rounded-[12px] border border-line bg-panel px-4 py-3.5">
        <div className="flex items-center gap-2">
          <Icon name="shield" size={14} className="text-amber" />
          <p className="m-0 text-[12.5px] font-semibold text-ink">
            The model declined this request
          </p>
          {error.category ? <Badge tone="amber">{error.category}</Badge> : null}
        </div>
        <p className="mt-1.5 text-[12px] leading-[1.6] text-muted">
          Security content occasionally trips a safety classifier. Rephrasing the question, or
          narrowing it to a specific advisory or item, usually works.
        </p>
        {error.message ? (
          <p className="mt-1 text-[11.5px] leading-[1.6] text-faint italic">{error.message}</p>
        ) : null}
      </div>
    )
  }

  const copy = COPY[error.type] ?? { title: 'Something went wrong', body: '', icon: 'warning' }
  return (
    <div className="rounded-[12px] border border-line bg-panel px-4 py-3.5">
      <div className="flex items-center gap-2">
        <Icon name={copy.icon} size={14} className="text-faint" />
        <p className="m-0 text-[12.5px] font-semibold text-ink">{copy.title}</p>
      </div>
      <p className="mt-1.5 text-[12px] leading-[1.6] text-muted">{copy.body || error.message}</p>
      {copy.body && error.message ? (
        <p className="mt-1 text-[11.5px] leading-[1.6] text-faint">{error.message}</p>
      ) : null}
    </div>
  )
}
