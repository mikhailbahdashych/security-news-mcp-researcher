import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import {
  createFeed,
  deleteFeed,
  feedLabel,
  feedsQueryKey,
  seedDefaultFeeds,
  updateFeed,
  type Feed,
} from '../../api/inbox'
import { ApiError } from '../../api/client'
import { parseUtc } from '../../lib/dates'
import Badge from '../ui/Badge'
import Button from '../ui/Button'
import Dialog from '../ui/Dialog'
import Input from '../ui/Input'

interface ManageFeedsProps {
  feeds: Feed[]
  onClose: () => void
}

function lastRefreshed(feed: Feed): string {
  if (!feed.last_fetched_at) {
    return 'never refreshed'
  }
  return `refreshed ${parseUtc(feed.last_fetched_at).toLocaleString()}`
}

/** Add, enable/disable and delete feeds; seed the defaults. */
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
    <Dialog
      title="Feeds"
      description="The sources the inbox pulls from."
      width="lg"
      onClose={onClose}
      footer={
        <Button variant="primary" onClick={onClose}>
          Done
        </Button>
      }
    >
      <form
        className="flex flex-wrap gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          if (url.trim()) {
            add.mutate(url.trim())
          }
        }}
      >
        <Input
          type="url"
          tone="bg"
          value={url}
          required
          aria-label="Feed URL"
          placeholder="https://example.com/feed.xml"
          onChange={(event) => setUrl(event.target.value)}
          className="min-w-[240px] flex-1"
        />
        <Button type="submit" variant="primary" loading={add.isPending} disabled={busy}>
          Add feed
        </Button>
        <Button
          loading={seed.isPending}
          disabled={busy}
          onClick={() => seed.mutate()}
          title="Add the built-in security feeds that are not configured yet"
        >
          Seed defaults
        </Button>
      </form>

      {error ? <p className="mt-2 text-[11.5px] text-red">{error}</p> : null}

      {feeds.length === 0 ? (
        <p className="mt-4 text-[12px] text-muted">
          No feeds yet. Add one above, or press <strong className="text-ink">Seed defaults</strong>{' '}
          for a starter set of security sources.
        </p>
      ) : (
        <ul className="mt-4 flex max-h-[42vh] flex-col gap-1.5 overflow-y-auto">
          {feeds.map((feed) => (
            <li
              key={feed.id}
              className="flex flex-wrap items-start gap-3 rounded-[8px] border border-line bg-bg px-3 py-2"
            >
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-1.5">
                  <p className="truncate text-[12.5px] font-medium text-ink">{feedLabel(feed)}</p>
                  {feed.enabled ? null : <Badge>disabled</Badge>}
                  {feed.last_error ? <Badge tone="red">error</Badge> : null}
                </div>
                <p className="truncate text-[11px] text-faint">{feed.url}</p>
                <p className="mt-0.5 text-[11px] text-faint">{lastRefreshed(feed)}</p>
                {feed.last_error ? (
                  <p className="mt-0.5 text-[11px] text-red">{feed.last_error}</p>
                ) : null}
              </div>
              <div className="flex shrink-0 gap-1.5">
                <Button
                  size="sm"
                  disabled={busy}
                  onClick={() => toggle.mutate({ id: feed.id, enabled: !feed.enabled })}
                >
                  {feed.enabled ? 'Disable' : 'Enable'}
                </Button>
                <Button
                  size="sm"
                  variant="danger"
                  disabled={busy}
                  onClick={() => remove.mutate(feed.id)}
                  title="Deletes the feed and every item it brought in"
                >
                  Delete
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Dialog>
  )
}
