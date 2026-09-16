import BackendStatus from '../BackendStatus'
import ChatList from '../chat/ChatList'
import Icon, { type IconName } from './Icon'
import { cx } from './classes'
import { PAGE_KEYS, PAGE_LABELS, type PageKey } from './layout'
import { RAIL_WIDTH } from './railState'
import type { Theme } from './theme'

/** `⌘K` on a Mac, `Ctrl K` everywhere else — the hint has to match the binding. */
const SHORTCUT_HINT =
  typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.userAgent)
    ? '⌘K'
    : 'Ctrl K'

const NAV_ICONS: Record<PageKey, IconName> = {
  inbox: 'inbox',
  research: 'research',
  notes: 'notes',
  settings: 'settings',
}

/**
 * The top nav: the three destinations, in `PAGE_KEYS` order.
 *
 * Settings is not one of them — it sits in the bottom group with the theme and
 * the collapse toggles, because it is the app's own knobs rather than a fourth
 * place to look at security news. Derived from `PAGE_KEYS` rather than spelled
 * out, so a page added there appears here without a second edit; `PAGE_KEYS`
 * itself keeps its order for the layout selects.
 */
const TOP_NAV = PAGE_KEYS.filter((page) => page !== 'settings')

export interface RailProps {
  expanded: boolean
  onToggleExpanded: () => void
  theme: Theme
  onToggleTheme: () => void
  /** Which nav entries read as current: the URL's page, plus the split pane's. */
  isActive: (page: PageKey) => boolean
  /** Which nav entries have work running behind them, for the dot. */
  busyPages: Set<PageKey>
  onNavigate: (page: PageKey) => void
  onOpenSearch: () => void
  /**
   * Whether Research is on screen — in either pane. The chat history fills the
   * rail's middle only there: it is that page's list, and beside the Inbox it
   * would be a sidebar for somewhere else.
   */
  researchOpen: boolean
  /** The routed chat, so the history can mark it. Null on `/chat` and elsewhere. */
  chatSessionId: number | null
}

/**
 * The left rail.
 *
 * Collapsed to icons by default and widened on request, rather than hidden
 * behind a hamburger: the destinations never change, and at 58px they cost less
 * room than the button that would hide them.
 */
export default function Rail({
  expanded,
  onToggleExpanded,
  theme,
  onToggleTheme,
  isActive,
  busyPages,
  onNavigate,
  onOpenSearch,
  researchOpen,
  chatSessionId,
}: RailProps) {
  const themeLabel = theme === 'dark' ? 'Switch to light' : 'Switch to dark'
  const railLabel = expanded ? 'Collapse sidebar' : 'Expand sidebar'

  return (
    <aside
      style={{ width: RAIL_WIDTH[expanded ? 'expanded' : 'collapsed'] }}
      className="flex shrink-0 flex-col items-stretch gap-1.5 border-r border-line bg-panel px-[11px] py-[14px] transition-[width] duration-[180ms] ease-out"
    >
      <div className="mb-2 flex items-center gap-2.5">
        <div
          title="Security News Researcher"
          className="flex size-[34px] shrink-0 items-center justify-center rounded-[10px] bg-accent-btn text-on-accent"
        >
          <Icon name="shield" size={18} />
        </div>
        {expanded ? (
          <div className="min-w-0">
            <p className="text-[12.5px] font-semibold whitespace-nowrap">Security News</p>
            <p className="text-[10.5px] whitespace-nowrap text-faint">Researcher</p>
          </div>
        ) : null}
      </div>

      <RailButton
        expanded={expanded}
        icon="search"
        label="Search"
        title={`Search  ${SHORTCUT_HINT}`}
        onClick={onOpenSearch}
        trailing={expanded ? SHORTCUT_HINT : undefined}
      />

      <div className="mx-1 my-1 h-px bg-line" />

      <nav className="flex flex-col gap-1.5">
        {TOP_NAV.map((page) => (
          <RailButton
            key={page}
            expanded={expanded}
            icon={NAV_ICONS[page]}
            label={PAGE_LABELS[page]}
            title={PAGE_LABELS[page]}
            active={isActive(page)}
            busy={busyPages.has(page)}
            onClick={() => onNavigate(page)}
          />
        ))}
      </nav>

      {/* Collapsed there is nowhere to put it, and 58px of truncated titles
          would say nothing: the rail goes back to being four icons. */}
      {expanded && researchOpen ? <ChatList activeId={chatSessionId} /> : null}

      <div className="mt-auto flex flex-col gap-1.5 pt-1.5">
        <RailButton
          expanded={expanded}
          icon={NAV_ICONS.settings}
          label={PAGE_LABELS.settings}
          title={PAGE_LABELS.settings}
          active={isActive('settings')}
          busy={busyPages.has('settings')}
          onClick={() => onNavigate('settings')}
        />
        <RailButton
          expanded={expanded}
          icon={theme === 'dark' ? 'sun' : 'moon'}
          label={themeLabel}
          title={themeLabel}
          onClick={onToggleTheme}
        />
        <RailButton
          expanded={expanded}
          icon={expanded ? 'collapse' : 'expand'}
          label="Collapse"
          title={railLabel}
          onClick={onToggleExpanded}
        />
        <BackendStatus expanded={expanded} />
      </div>
    </aside>
  )
}

interface RailButtonProps {
  expanded: boolean
  icon: IconName
  label: string
  title: string
  active?: boolean
  /** Something is running on that page while the user is looking elsewhere. */
  busy?: boolean
  trailing?: string
  onClick: () => void
}

/**
 * One rail entry, in both shapes: a 36px icon square when collapsed, a
 * full-width row when expanded. `title` is always set, because collapsed the
 * icon is the only thing there is to read.
 */
function RailButton({
  expanded,
  icon,
  label,
  title,
  active = false,
  busy = false,
  trailing,
  onClick,
}: RailButtonProps) {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      aria-current={active ? 'page' : undefined}
      onClick={onClick}
      className={cx(
        'flex items-center rounded-[10px] transition-colors duration-150',
        expanded
          ? 'h-[34px] w-full gap-2.5 px-[11px] text-left'
          : 'size-9 shrink-0 justify-center',
        active
          ? 'bg-accent-soft text-accent'
          : cx(expanded ? 'text-muted' : 'text-faint', 'hover:bg-hover hover:text-ink'),
      )}
    >
      {/* The dot is anchored to the icon, not to the button, so it lands in the
          same place whether the rail is 58px of icons or a 198px list. */}
      <span className="relative flex shrink-0">
        <Icon name={icon} size={18} />
        {busy ? (
          <span
            role="status"
            aria-label="A research turn is running"
            className="absolute -top-px -right-px size-1.5 rounded-full bg-accent-btn"
          />
        ) : null}
      </span>
      {expanded ? (
        <>
          <span className="text-[12.5px] whitespace-nowrap">{label}</span>
          {trailing ? (
            <span className="ml-auto text-[10px] text-faint">{trailing}</span>
          ) : null}
        </>
      ) : null}
    </button>
  )
}
