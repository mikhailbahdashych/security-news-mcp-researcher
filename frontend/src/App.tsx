import { NavLink, Route, Routes } from 'react-router-dom'

import BackendStatus from './components/BackendStatus'
import InboxPage from './pages/Inbox'
import NotesPage from './pages/Notes'
import ResearchPage from './pages/Research'
import SettingsPage from './pages/Settings'

const NAV = [
  { to: '/', label: 'Inbox' },
  { to: '/chat', label: 'Research' },
  { to: '/notes', label: 'Notes' },
  { to: '/settings', label: 'Settings' },
]

export default function App() {
  return (
    <div className="flex h-full bg-white text-slate-900">
      <aside className="flex w-56 shrink-0 flex-col border-r border-slate-200 bg-slate-50 px-4 py-6">
        <div className="px-3">
          <p className="text-sm font-semibold tracking-tight">Security News</p>
          <p className="text-xs text-slate-500">Researcher</p>
        </div>

        <nav className="mt-8 flex flex-col gap-1">
          {NAV.map(({ to, label }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                `rounded-md px-3 py-2 text-sm transition-colors ${
                  isActive
                    ? 'bg-slate-900 font-medium text-white'
                    : 'text-slate-600 hover:bg-slate-200/60 hover:text-slate-900'
                }`
              }
            >
              {label}
            </NavLink>
          ))}
        </nav>

        <div className="mt-auto px-3 pt-6">
          <BackendStatus />
        </div>
      </aside>

      <main className="flex-1 overflow-y-auto">
        <Routes>
          <Route path="/" element={<InboxPage />} />
          <Route path="/chat" element={<ResearchPage />} />
          <Route path="/notes" element={<NotesPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </main>
    </div>
  )
}
