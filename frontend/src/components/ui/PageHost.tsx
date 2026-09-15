import ChatPage from '../../pages/ChatPage'
import InboxPage from '../../pages/Inbox'
import NotesPage from '../../pages/Notes'
import SettingsPage from '../../pages/Settings'
import type { PageKey } from './layout'

/**
 * The contract every top-level page implements.
 *
 * `embedded` means "you are the right-hand pane of the split view": there is no
 * route for you. A page in that mode must keep its own selection in React state
 * and must not read `useParams`, set the URL, or react to the query string —
 * all of those belong to the left pane, and a right pane that followed them
 * would just mirror it.
 *
 * One carve-out: Settings' **Left pane** control. It is not steering its own
 * content, it is the switch for the *other* pane — the routed one — so it reads
 * `useLocation` to name what is over there and navigates to move it, embedded or
 * not. Moving the left pane is navigation by definition; there is no second URL
 * scheme to move it any other way. Nothing else in an embedded page may touch
 * the router.
 *
 * Everything else — data fetching, dialogs, mutations — is identical in both
 * modes.
 */
export interface EmbeddablePageProps {
  embedded?: boolean
}

export interface PageHostProps {
  page: PageKey
  embedded?: boolean
}

/** Renders a page by key, for the split view's right pane. */
export default function PageHost({ page, embedded = false }: PageHostProps) {
  switch (page) {
    case 'inbox':
      return <InboxPage embedded={embedded} />
    case 'research':
      return <ChatPage embedded={embedded} />
    case 'notes':
      return <NotesPage embedded={embedded} />
    case 'settings':
      return <SettingsPage embedded={embedded} />
  }
}
