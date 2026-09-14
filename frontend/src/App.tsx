import { useEffect, useRef, useState } from 'react'
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
  usePaneASync(layout.split, layout.paneA, urlPage, layout.setPaneA)

  const goTo = (page: PageKey) => {
    if (layout.split) {
      layout.setPaneA(page)
    }
    navigate(routeForPage(page))
  }

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
        <div
          className={cx(
            'h-full min-w-0 flex-1 overflow-y-auto',
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
          <div className="h-full min-w-0 flex-1 overflow-y-auto">
            <PageHost page={layout.paneB} embedded />
          </div>
        ) : null}
      </main>

      <GlobalSearch open={searchOpen} onOpenChange={setSearchOpen} />
    </div>
  )
}

/**
 * Keeps the stored left pane and the URL saying the same thing.
 *
 * Two things can move: the rail or a link changes the URL, or the Settings page
 * changes `paneA`. Which one moved decides who follows, so the refs remember
 * the last values seen rather than guessing from the current ones — that is
 * also what stops a deep link being overwritten by a stale stored pane on the
 * very first render.
 */
function usePaneASync(
  split: boolean,
  paneA: PageKey,
  urlPage: PageKey,
  setPaneA: (page: PageKey) => void,
) {
  const navigate = useNavigate()
  const lastSplit = useRef(split)
  const lastPaneA = useRef(paneA)
  const lastUrl = useRef(urlPage)

  // No dependency list: `layout` is a fresh object every render, so the guards
  // below are the real condition. Every branch is idempotent, and the render it
  // may trigger settles on the next pass.
  useEffect(() => {
    const splitChanged = split !== lastSplit.current
    lastSplit.current = split

    if (!split) {
      lastPaneA.current = paneA
      lastUrl.current = urlPage
      return
    }

    // Turning the split on adopts whatever is already on screen.
    if (splitChanged) {
      lastPaneA.current = urlPage
      lastUrl.current = urlPage
      if (paneA !== urlPage) {
        setPaneA(urlPage)
      }
      return
    }

    // Settings moved the left pane: the URL follows it.
    if (paneA !== lastPaneA.current) {
      lastPaneA.current = paneA
      lastUrl.current = urlPage
      if (paneA !== urlPage) {
        navigate(routeForPage(paneA))
      }
      return
    }

    // The rail or a link moved the URL: the stored pane follows it.
    if (urlPage !== lastUrl.current) {
      lastUrl.current = urlPage
      if (paneA !== urlPage) {
        lastPaneA.current = urlPage
        setPaneA(urlPage)
      }
    }
  })
}
