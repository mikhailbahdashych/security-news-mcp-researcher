import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import {
  createFeed,
  deleteFeed,
  feedLabel,
  feedsQueryKey,
  parseUtc,
  seedDefaultFeeds,
  updateFeed,
  type Feed,
} from '../../api/inbox'
import { ApiError } from '../../api/client'

interface ManageFeedsProps {
  feeds: Feed[]
  onClose: () => void
}

const buttonClass =
  'rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 ' +
  'hover:border-slate-400 disabled:opacity-40'

function lastRefreshed(feed: Feed): string {
  if (!feed.last_fetched_at) {
    return 'never refreshed'
  }
  return `refreshed ${parseUtc(feed.last_fetched_at).toLocaleString()}`
}

/** Add, rename-by-disabling, enable/disable and delete feeds; seed the defaults. */
export default function ManageFeeds({ feeds, onClose }: ManageFeedsProps) {
  const queryClient = useQueryClient()
  const [url, setUrl] = useState('')
  const [error, setError] = useState<string | null>(null)

  const invalidate = () => queryClient.invalidateQueries({ queryKey: feedsQueryKey })

  const add = useMutation({
    mutationFn: (feedUrl: string) => createFeed({ url: feedUrl }),
    onSuccess: async () => {
      setUrl('')
      setError(null)
      await invalidate()
    },
    onError: (cause: unknown) => {
      setError(
        cause instanceof ApiError
          ? cause.detail
          : 'Could not add that feed. Is the backend running?',
      )
    },
  })

  const seed = useMutation({ mutationFn: seedDefaultFeeds, onSuccess: invalidate })
  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) => updateFeed(id, { enabled }),
    onSuccess: invalidate,
  })
  const remove = useMutation({ mutationFn: deleteFeed, onSuccess: invalidate })

  const busy = add.isPending || seed.isPending || toggle.isPending || remove.isPending

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-900">Feeds</h2>
        <button
          type="button"
          onClick={onClose}
          className="text-xs text-slate-500 hover:text-slate-900"
        >
          Done
        </button>
      </div>

      <form
        className="mt-3 flex flex-wrap gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          if (url.trim()) {
            add.mutate(url.trim())
          }
        }}
      >
        <input
          type="url"
          value={url}
          required
          placeholder="https://example.com/feed.xml"
          onChange={(event) => setUrl(event.target.value)}
          className="min-w-64 flex-1 rounded-md border border-slate-300 px-3 py-1.5 text-xs outline-none focus:border-slate-500"
        />
        <button type="submit" disabled={busy} className={buttonClass}>
          Add feed
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => seed.mutate()}
          className={buttonClass}
          title="Add the built-in security feeds that are not configured yet"
        >
          Seed defaults
        </button>
      </form>

      {error ? <p className="mt-2 text-xs text-rose-600">{error}</p> : null}

      {feeds.length === 0 ? (
        <p className="mt-4 text-xs text-slate-500">
          No feeds yet. Add one above, or press <strong>Seed defaults</strong> for a starter set of
          security sources.
        </p>
      ) : (
        <ul className="mt-4 divide-y divide-slate-100">
          {feeds.map((feed) => (
            <li key={feed.id} className="flex flex-wrap items-start gap-3 py-2.5">
              <div className="min-w-0 flex-1">
                <p className="truncate text-xs font-medium text-slate-900">{feedLabel(feed)}</p>
                <p className="truncate text-[11px] text-slate-500">{feed.url}</p>
                <p className="mt-0.5 text-[11px] text-slate-500">
                  {feed.enabled ? 'enabled' : 'disabled'} · {lastRefreshed(feed)}
                </p>
                {feed.last_error ? (
                  <p className="mt-0.5 text-[11px] text-rose-600">{feed.last_error}</p>
                ) : null}
              </div>
              <div className="flex gap-1.5">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => toggle.mutate({ id: feed.id, enabled: !feed.enabled })}
                  className={buttonClass}
                >
                  {feed.enabled ? 'Disable' : 'Enable'}
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => remove.mutate(feed.id)}
                  className={`${buttonClass} text-rose-600`}
                  title="Deletes the feed and every item it brought in"
                >
                  Delete
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
