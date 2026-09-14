/**
 * The shared class strings.
 *
 * Every primitive in this folder is built from these, and pages reach for them
 * directly when they need a design-system surface that is not a component (an
 * `<a>` styled as a button, a row inside a list card). Keeping the strings in
 * one module is what stops four page agents inventing four slightly different
 * secondary buttons.
 *
 * Colours are token utilities only (`bg-panel`, `text-muted`, …) so both themes
 * come out right; the raw hexes live in `index.css` and nowhere else.
 */

/** Joins class names, dropping anything falsy. */
export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(' ')
}

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'
export type ButtonSize = 'sm' | 'md'

const BUTTON_BASE =
  'inline-flex items-center justify-center gap-1.5 rounded-[8px] font-medium ' +
  'transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-40'

const BUTTON_SIZES: Record<ButtonSize, string> = {
  sm: 'px-2.5 py-1 text-[11.5px]',
  md: 'px-3 py-1.5 text-[12px]',
}

const BUTTON_VARIANTS: Record<ButtonVariant, string> = {
  primary: 'bg-accent-btn text-on-accent hover:opacity-90',
  secondary: 'border border-line bg-panel text-ink hover:bg-hover',
  ghost: 'text-muted hover:bg-hover hover:text-ink',
  danger: 'text-faint hover:text-red',
}

export function buttonClass(
  variant: ButtonVariant = 'secondary',
  size: ButtonSize = 'md',
  extra?: string,
): string {
  return cx(BUTTON_BASE, BUTTON_SIZES[size], BUTTON_VARIANTS[variant], extra)
}

/** Inputs sit on `panel` out in the open and on `bg` when they are inside a card. */
export type ControlTone = 'panel' | 'bg'

export function controlClass(tone: ControlTone = 'panel', extra?: string): string {
  return cx(
    'w-full rounded-[8px] border border-line px-3 py-1.5 text-[12px] text-ink outline-none',
    'transition-colors duration-150 focus:border-accent disabled:opacity-40',
    tone === 'bg' ? 'bg-bg' : 'bg-panel',
    extra,
  )
}

/** The page-level card every list, section and article sits in. */
export const CARD = 'rounded-[12px] border border-line bg-panel'

/** A row inside a card: pointer feedback without a colour change of its own. */
export const HOVER_ROW = 'transition-colors duration-150 hover:bg-hover'

/** `SOURCES`, `FROM THE INBOX` — the small all-caps signposts. */
export const SECTION_LABEL =
  'text-[10.5px] font-semibold tracking-[0.06em] uppercase text-faint'

/** Page container widths, per the design: Settings is narrower than the rest. */
export const PAGE_WIDTH = {
  default: 'max-w-[860px]',
  settings: 'max-w-[720px]',
  note: 'max-w-[760px]',
  answer: 'max-w-[720px]',
} as const

export const PAGE_CONTAINER = 'mx-auto flex w-full flex-col gap-3.5 px-7 pt-7 pb-10'

/** A scroll container for a whole page inside a shell pane. */
export const PAGE_SCROLL = 'h-full overflow-y-auto bg-bg text-ink'

export const PAGE_TITLE = 'font-display text-[24px] font-semibold tracking-[-0.01em]'
export const PAGE_SUBTITLE = 'text-[12px] text-muted'

/** Section heading inside a card (Settings' `Model`, `Tools`, …). */
export const CARD_TITLE = 'text-[13.5px] font-semibold text-ink'
export const CARD_DESCRIPTION = 'text-[12px] text-muted'

/** Field label above an input. */
export const FIELD_LABEL = 'text-[11.5px] font-medium text-muted'
export const FIELD_HINT = 'text-[11.5px] text-faint'

/** The chat composer and the ⌘K panel are the only two raised surfaces. */
export const COMPOSER =
  'rounded-[14px] border border-line bg-panel shadow-[0_2px_8px_rgba(0,0,0,0.04)]'
export const OVERLAY_PANEL =
  'rounded-[14px] border border-line bg-panel shadow-[0_16px_48px_rgba(0,0,0,0.25)]'
export const OVERLAY_BACKDROP = 'fixed inset-0 z-50 bg-[rgba(0,0,0,0.35)]'

/** Round pills: suggestion chips, attach buttons, source chips. */
export const PILL = 'rounded-full border border-line bg-panel px-3 py-[5px] text-[12px] text-muted'
export const PILL_DASHED =
  'rounded-full border border-dashed border-line px-2.5 py-[3px] text-[11px] text-muted ' +
  'transition-colors duration-150 hover:text-ink'
