import { useMutation, useQueryClient } from '@tanstack/react-query'

import {
  deleteSession,
  renameSession,
  sessionQueryKey,
  sessionsQueryKey,
  setSessionArchived,
} from '../../api/chat'

export interface SessionActions {
  rename: (id: number, title: string) => void
  archive: (id: number, archived: boolean) => void
  /** Rejects if the delete failed, so a confirm dialog can stay open and say so. */
  remove: (id: number) => Promise<void>
  /** The row with a write in flight, so a list can grey it out while it saves. */
  busyId: number | null
}

/**
 * Rename, archive and delete a chat — the three writes, in one place.
 *
 * Two lists offer them now (the rail's history and the archived-chats dialog in
 * Settings), and neither owns the conversation they act on, so the rules live
 * here rather than in whichever component happened to grow them first.
 *
 * Every one of them **invalidates rather than patches the cache**: a rename
 * reorders nothing but an archive changes which list a row belongs to, and a
 * delete changes it more thoroughly still. `sessionsQueryKey` is the prefix
 * every filtered variant hangs off, so one invalidation refreshes whichever
 * filter happens to be on screen.
 */
export function useSessionActions(): SessionActions {
  const queryClient = useQueryClient()

  const rename = useMutation({
    mutationFn: ({ id, title }: { id: number; title: string }) => renameSession(id, title),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: sessionsQueryKey }),
  })

  const archive = useMutation({
    mutationFn: ({ id, archived }: { id: number; archived: boolean }) =>
      setSessionArchived(id, archived),
    onSuccess: (_data, { id }) => {
      queryClient.invalidateQueries({ queryKey: sessionsQueryKey })
      // And the chat's own row, for the same reason a delete does it: archiving
      // takes the chat out of every list the rail shows, and the page that has
      // it open has to find out. `sessionQueryKey` is not under the
      // `['sessions']` prefix, so without this the research page went on
      // composing into a chat nothing listed until the next window focus.
      queryClient.invalidateQueries({ queryKey: sessionQueryKey(id) })
    },
  })

  const remove = useMutation({
    mutationFn: (id: number) => deleteSession(id),
    onSuccess: (_data, id) => {
      queryClient.invalidateQueries({ queryKey: sessionsQueryKey })
      // And the detail query for the chat that was deleted, which is how the
      // research page finds out. Deleting the chat it has open used to be the
      // list's business — it reached into the page, abandoned the live turn and
      // navigated. It cannot now: the list is in the rail and the page may even
      // be the other pane. So this refetch answers 404, and `ChatPage`'s
      // missing-session effect does what it already does for a chat deleted in
      // another tab: abandon the turn, reset the live state and leave the URL.
      queryClient.invalidateQueries({ queryKey: sessionQueryKey(id) })
    },
  })

  return {
    rename: (id, title) => rename.mutate({ id, title }),
    archive: (id, archived) => archive.mutate({ id, archived }),
    remove: (id) => remove.mutateAsync(id).then(() => undefined),
    busyId:
      (rename.isPending ? rename.variables?.id : undefined) ??
      (archive.isPending ? archive.variables?.id : undefined) ??
      (remove.isPending ? remove.variables : undefined) ??
      null,
  }
}
