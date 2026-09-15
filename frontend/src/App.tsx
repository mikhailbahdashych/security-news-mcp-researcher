import { useState } from 'react'
import { Route, Routes, useLocation, useNavigate } from 'react-router-dom'

import GlobalSearch from './components/ui/GlobalSearch'
import PageHost from './components/ui/PageHost'
import Rail from './components/ui/Rail'
import { cx } from './components/ui/classes'
import { pageFromPath, routeForPage, useLayout, type PageKey } from './components/ui/layout'
import { useRail } from './components/ui/railState'
import { useTheme } from './components/ui/theme'
import ChatPage from './pages/ChatPage'
import InboxPage from './pages/Inbox'
import NoteDetailPage from './pages/NoteDetail'
import NotesPage from './pages/Notes'
import SettingsPage from './pages/Settings'

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

  const urlPage = pageFromPath(location.pathname)

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
        isActive={(page) => urlPage === page || (layout.split && layout.paneB === page)}
        onNavigate={goTo}
        onOpenSearch={() => setSearchOpen(true)}
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
          <Routes>
            <Route path="/" element={<InboxPage />} />
            <Route path="/chat" element={<ChatPage />} />
            <Route path="/chat/:id" element={<ChatPage />} />
            <Route path="/notes" element={<NotesPage />} />
            <Route path="/notes/:id" element={<NoteDetailPage />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Routes>
        </div>

        {layout.split ? (
          <div className="h-full min-w-0 flex-1 overflow-hidden">
            <PageHost page={layout.paneB} embedded />
          </div>
        ) : null}
      </main>

      <GlobalSearch open={searchOpen} onOpenChange={setSearchOpen} />
    </div>
  )
}
