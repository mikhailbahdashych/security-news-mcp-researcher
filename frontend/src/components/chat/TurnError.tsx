import type { ErrorPayload } from '../../api/chat'

const COPY: Record<string, { title: string; body: string }> = {
  turn_limit: {
    title: 'The assistant stopped early',
    body: 'It reached the tool-call limit for one turn. Ask a narrower question, or raise the limit in Settings.',
  },
  max_tokens: {
    title: 'The answer was cut off',
    body: 'The response hit the output limit before it finished.',
  },
  rate_limit: {
    title: 'Rate limited',
    body: 'The API asked us to slow down. Wait a moment and try again.',
  },
  cancelled: { title: 'Stopped', body: 'You stopped this turn. Anything already written is kept.' },
  connection: {
    title: 'Connection problem',
    body: 'The request to the API could not be completed.',
  },
  api_error: { title: 'The API returned an error', body: '' },
}

/**
 * A refusal is not a failure, and must not look like one.
 *
 * Opus 5 ships elevated cybersecurity safeguards and this app's whole domain is
 * security content, so benign questions occasionally trip a classifier. It gets
 * its own calm treatment — and it is never folded into the transcript as if the
 * assistant had answered.
 */
export default function TurnError({ error }: { error: ErrorPayload }) {
  if (error.type === 'refusal') {
    return (
      <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
        <p className="font-medium">The model declined this request</p>
        {error.category ? (
          <p className="mt-0.5 text-xs text-amber-800">category: {error.category}</p>
        ) : null}
        <p className="mt-1 text-xs leading-relaxed text-amber-800">
          Security content occasionally trips a safety classifier. Rephrasing the question, or
          narrowing it to a specific advisory or item, usually works.
        </p>
        {error.message ? (
          <p className="mt-1 text-xs italic text-amber-700">{error.message}</p>
        ) : null}
      </div>
    )
  }

  const copy = COPY[error.type] ?? { title: 'Something went wrong', body: '' }
  return (
    <div className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-900">
      <p className="font-medium">{copy.title}</p>
      <p className="mt-0.5 text-xs leading-relaxed text-rose-800">{copy.body || error.message}</p>
    </div>
  )
}
