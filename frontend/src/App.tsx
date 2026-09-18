import { useState } from 'react'
import { Route, Routes, useLocation, useMatch, useNavigate } from 'react-router-dom'

import { parseSessionId } from './api/chat'
import ErrorBoundary from './components/ui/ErrorBoundary'
import GlobalSearch from './components/ui/GlobalSearch'
import PageHost from './components/ui/PageHost'
import Rail from './components/ui/Rail'
import { cx } from './components/ui/classes'
import { pageFromPath, routeForPage, useLayout, type PageKey } from './components/ui/layout'
import { useRail } from './components/ui/railState'
import { useTheme } from './components/ui/theme'
import { useRunningTurns } from './lib/useRunningTurns'
import ChatPage from './pages/ChatPage'
import InboxPage from './pages/Inbox'
import KnowledgePage from './pages/Knowledge'
import NoteDetailPage from './pages/NoteDetail'
import NotesPage from './pages/Notes'
import SettingsPage from './pages/Settings'

/**
 * The rail's dot is a statement about the Research page, not about a session:
 * two stable sets rather than one built per render, so the rail is not handed a
 * new object every time anything above it re-renders.
 */
const RESEARCH_BUSY = new Set<PageKey>(['research'])
const NOTHING_BUSY = new Set<PageKey>()

/**
 * The app shell: rail on the left, one or two page panes on the right, the ⌘K
 * overlay above everything.
 *
 * In split mode the LEFT pane is still the router — it has the URL, the back
 * button and every deep link — and only the right pane is a second, embedded
 * copy of a page. That asymmetry is deliberate: two routable panes would need a
 * URL scheme of their own, and nothing here is worth that.
 */
export default function App() {
  const { expanded, toggle: toggleRail } = useRail()
  const { theme, toggle: toggleTheme } = useTheme()
  const layout = useLayout()
  const location = useLocation()
  const navigate = useNavigate()
  const [searchOpen, setSearchOpen] = useState(false)
  const running = useRunningTurns()

  const urlPage = pageFromPath(location.pathname)
  // Which chat the URL names, for the rail's history list. `useMatch` rather
  // than a `pathname.startsWith` of its own, so the one place that decides what
  // `/chat/:id` means is the router — and `parseSessionId`, because `:id`
  // matches anything and only a positive integer is an id.
  const chatMatch = useMatch('/chat/:id')
  const chatSessionId = parseSessionId(chatMatch?.params.id)

  // A page is current if the URL shows it, or if the split view's second pane
  // does. Named rather than inlined because the rail asks it twice.
  const isActive = (page: PageKey) => urlPage === page || (layout.split && layout.paneB === page)

  // The left pane is the router's pane, so moving it *is* navigating: there is
  // no stored copy of which page it shows, and nothing to keep in step.
  const goTo = (page: PageKey) => navigate(routeForPage(page))

  return (
    <div className="flex h-screen overflow-hidden bg-bg text-ink">
      <Rail
        expanded={expanded}
        onToggleExpanded={toggleRail}
        theme={theme}
        onToggleTheme={toggleTheme}
        isActive={isActive}
        busyPages={running.size > 0 ? RESEARCH_BUSY : NOTHING_BUSY}
        onNavigate={goTo}
        onOpenSearch={() => setSearchOpen(true)}
        researchOpen={isActive('research')}
        chatSessionId={chatSessionId}
      />

      <main className="flex min-w-0 flex-1">
        {/* The pane clips; the page inside it scrolls (`PAGE_SCROLL`, or the
            research view's own column). Two nested scrollports meant two
            scrollbars and a wheel that sometimes moved the wrong one. */}
        <div
          className={cx(
            'h-full min-w-0 flex-1 overflow-hidden',
            layout.split && 'border-r border-line',
          )}
        >
          {/* One boundary per pane, so a page that cannot render costs its own
              pane and not the rail, the other pane and the ⌘K overlay with it.
              A boundary that has caught stays caught, and navigating somewhere
              else has to be a fresh attempt — otherwise one bad payload wedges
              the app until a reload. The routed pane gets that from `resetKey`,
              NOT from `key`: a key on the pathname remounts the page on every
              in-page navigation, which drops the Knowledge filters and the
              first question of a new chat. */}
          <ErrorBoundary resetKey={location.pathname}>
            <Routes>
              <Route path="/" element={<InboxPage />} />
              <Route path="/chat" element={<ChatPage />} />
              <Route path="/chat/:id" element={<ChatPage />} />
              <Route path="/notes" element={<NotesPage />} />
              <Route path="/notes/:id" element={<NoteDetailPage />} />
              <Route path="/knowledge" element={<KnowledgePage />} />
              <Route path="/knowledge/:id" element={<KnowledgePage />} />
              <Route path="/settings" element={<SettingsPage />} />
            </Routes>
          </ErrorBoundary>
        </div>

        {layout.split ? (
          <div className="h-full min-w-0 flex-1 overflow-hidden">
            <ErrorBoundary key={layout.paneB}>
              <PageHost page={layout.paneB} embedded />
            </ErrorBoundary>
          </div>
        ) : null}
      </main>

      <GlobalSearch open={searchOpen} onOpenChange={setSearchOpen} />
    </div>
  )
}
