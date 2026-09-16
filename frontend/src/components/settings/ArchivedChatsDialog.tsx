import { useInfiniteQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import {
  fetchSessions,
  sessionsListKey,
  whenLabel,
  type SessionFilters,
} from '../../api/chat'
import useDebouncedValue from '../../lib/useDebouncedValue'
import { useSessionActions } from '../chat/useSessionActions'
import Button from '../ui/Button'
import Dialog from '../ui/Dialog'
import EmptyState from '../ui/EmptyState'
import Input from '../ui/Input'
import { HOVER_ROW, cx } from '../ui/classes'

export interface ArchivedChatsDialogProps {
  onClose: () => void
}

/**
 * The archived chats, and the two things worth doing to one.
 *
 * Archiving used to be a checkbox at the foot of the history drawer, which
 * silently swapped the contents of the list: nothing on a row said which of the
 * two lists you were looking at, and the archived ones were in the way of the
 * live ones the rest of the time. So the rail shows the live chats only, and
 * the archive is a drawer of its own — here, where the other rarely-used knobs
 * are.
 */
export default function ArchivedChatsDialog({ onClose }: ArchivedChatsDialogProps) {
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  const debouncedSearch = useDebouncedValue(search)
  const { archive, busyId } = useSessionActions()

  const filters = useMemo<SessionFilters>(
    () => ({ q: debouncedSearch, archived: 'true' }),
    [debouncedSearch],
  )

  const sessions = useInfiniteQuery({
    queryKey: sessionsListKey(filters),
    queryFn: ({ pageParam }) => fetchSessions(filters, pageParam as string | undefined),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  })

  const rows = sessions.data?.pages.flatMap((page) => page.sessions) ?? []

  // Settings is the one page that navigates from inside the embedded pane — it
  // edits app state rather than a selection, and this is the same carve-out its
  // "Left pane" select lives under. Opening an archived chat routes, which is
  // what puts it in front of the user whichever pane Research is in.
  const open = (id: number) => {
    navigate(`/chat/${id}`)
    onClose()
  }

  return (
    <Dialog
      title="Archived chats"
      description="Opening one does not unarchive it."
      onClose={onClose}
      footer={<Button onClick={onClose}>Close</Button>}
    >
      <Input
        type="search"
        autoFocus
        tone="bg"
        value={search}
        placeholder="Filter archived chats…"
        aria-label="Filter archived chats"
        onChange={(event) => setSearch(event.target.value)}
      />

      <div className="mt-3 max-h-[46vh] overflow-y-auto">
        {sessions.isPending ? (
          <EmptyState title="Loading chats…" />
        ) : sessions.isError ? (
          <EmptyState
            tone="error"
            icon="warning"
            title="Could not load chats."
            description="Is the backend running?"
          />
        ) : rows.length === 0 ? (
          <EmptyState title="No archived chats." />
        ) : (
          <ul className="m-0 flex list-none flex-col p-0">
            {rows.map((session) => (
              <li
                key={session.id}
                className={cx(
                  'flex items-center gap-3 rounded-[8px] px-2 py-1.5',
                  HOVER_ROW,
                  busyId === session.id && 'opacity-50',
                )}
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[12.5px] text-ink">
                    {session.title || 'Untitled chat'}
                  </p>
                  <p className="mt-px text-[10.5px] text-faint">{whenLabel(session.updated_at)}</p>
                </div>
                <div className="flex shrink-0 items-center gap-1.5">
                  <Button
                    size="sm"
                    // Both actions go down with the row while it is saving:
                    // opening a chat whose unarchive is still in flight raced
                    // the navigation against the invalidation.
                    disabled={busyId === session.id}
                    onClick={() => open(session.id)}
                  >
                    Open
                  </Button>
                  <Button
                    size="sm"
                    disabled={busyId === session.id}
                    // The row leaves this list the moment the server answers:
                    // the shared hook invalidates `sessionsQueryKey`, which is
                    // the prefix both this query and the rail's hang off, so
                    // the chat reappears in the sidebar at the same time.
                    onClick={() => archive(session.id, false)}
                  >
                    Unarchive
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}

        {sessions.hasNextPage ? (
          <div className="px-2 pt-2">
            <Button
              size="sm"
              loading={sessions.isFetchingNextPage}
              onClick={() => void sessions.fetchNextPage()}
              className="w-full"
            >
              Load more
            </Button>
          </div>
        ) : null}
      </div>
    </Dialog>
  )
}
