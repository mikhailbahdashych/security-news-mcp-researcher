import type { ReactNode } from 'react'

/**
 * The icon set, inline.
 *
 * No icon library: the design's line work is one weight (1.6) at one size in one
 * 20×20 box, and a dependency would bring thousands of glyphs drawn to somebody
 * else's grid. The nav, theme, rail, search, logo and send glyphs are the design
 * prototype's own paths, verbatim; the rest are drawn to match. (`settings` was
 * one of them until it had to stop being a sun — see below.)
 *
 * Icons are decorative by default (`aria-hidden`). An icon that is the only
 * content of a control needs a label on the control, not on the icon — see
 * `IconButton`.
 */

interface Glyph {
  body: ReactNode
  /** Two glyphs come from the prototype on a 16 grid; the rest are 20. */
  viewBox?: string
  strokeWidth?: number
  /** Solid glyphs (the filled star) paint rather than stroke. */
  filled?: boolean
  /** Spinners turn. */
  spin?: boolean
}

const GLYPHS = {
  // --- prototype paths, verbatim -------------------------------------------
  inbox: {
    body: (
      <>
        <path d="M3 4h14v12H3z" />
        <path d="M3 12h4l1.5 2h3L13 12h4" />
      </>
    ),
  },
  research: {
    body: (
      <>
        <path d="M17 10a7 7 0 1 0-3 5.7L17 17l-.6-3.2A7 7 0 0 0 17 10z" />
        <path d="M7 8.5h6M7 11.5h4" />
      </>
    ),
  },
  notes: {
    body: (
      <>
        <path d="M5 3h8l3 3v11H5z" />
        <path d="M13 3v3h3M8 10h5M8 13h5" />
      </>
    ),
  },
  // A cog, not a sun. The prototype's `settings` glyph was a circle with eight
  // detached rays — the same drawing as `sun` two entries down, so the rail's
  // bottom group read as "light mode" twice. This one is a single closed outline
  // whose eight teeth are attached to the body, on a 24 grid because the teeth
  // need the extra room to survive at 18px. `strokeWidth` is scaled with the
  // grid (1.6 x 24/20) so it still paints at the set's one weight.
  //
  // The number that decides whether it reads as a gear is the **notch base**:
  // the chord between two teeth where they meet the body. A first pass put the
  // flanks 2.0 units apart there — 1.5px at size 18, under a 1.42px stroke — so
  // every notch closed to a V and the glyph read as a bumpy disc. Teeth are
  // narrow (16 degrees at the root, 14 at the tip) and the body is small
  // (r 6.2 against tips at 9.3), which leaves a 3.1-unit notch base and a
  // 3.1-unit tooth depth: both comfortably over the stroke, at every size the
  // app draws this at.
  settings: {
    viewBox: '0 0 24 24',
    strokeWidth: 1.9,
    body: (
      <>
        <path d="M11.14 5.86 L10.87 2.77 L13.13 2.77 L12.86 5.86 A6.2 6.2 0 0 1 15.73 7.05 L17.73 4.67 L19.33 6.27 L16.95 8.27 A6.2 6.2 0 0 1 18.14 11.14 L21.23 10.87 L21.23 13.13 L18.14 12.86 A6.2 6.2 0 0 1 16.95 15.73 L19.33 17.73 L17.73 19.33 L15.73 16.95 A6.2 6.2 0 0 1 12.86 18.14 L13.13 21.23 L10.87 21.23 L11.14 18.14 A6.2 6.2 0 0 1 8.27 16.95 L6.27 19.33 L4.67 17.73 L7.05 15.73 A6.2 6.2 0 0 1 5.86 12.86 L2.77 13.13 L2.77 10.87 L5.86 11.14 A6.2 6.2 0 0 1 7.05 8.27 L4.67 6.27 L6.27 4.67 L8.27 7.05 A6.2 6.2 0 0 1 11.14 5.86 Z" />
        <circle cx="12" cy="12" r="2.9" />
      </>
    ),
  },
  sun: {
    body: (
      <>
        <circle cx="10" cy="10" r="3.5" />
        <path d="M10 2v2M10 16v2M2 10h2M16 10h2M4.3 4.3l1.4 1.4M14.3 14.3l1.4 1.4M15.7 4.3l-1.4 1.4M5.7 14.3l-1.4 1.4" />
      </>
    ),
  },
  moon: {
    body: <path d="M16 12.5A7 7 0 0 1 7.5 4 7 7 0 1 0 16 12.5z" />,
  },
  expand: {
    body: (
      <>
        <rect x="3" y="3.5" width="14" height="13" rx="2" />
        <path d="M8 3.5v13" />
        <path d="M11.5 8l2 2-2 2" />
      </>
    ),
  },
  collapse: {
    body: (
      <>
        <rect x="3" y="3.5" width="14" height="13" rx="2" />
        <path d="M8 3.5v13" />
        <path d="M14 8l-2 2 2 2" />
      </>
    ),
  },
  search: {
    body: (
      <>
        <circle cx="9" cy="9" r="5.5" />
        <path d="M13.5 13.5L17 17" />
      </>
    ),
  },
  shield: {
    body: (
      <>
        <path d="M10 2l6.5 2.4v4.4c0 4.2-2.8 7.3-6.5 8.8-3.7-1.5-6.5-4.6-6.5-8.8V4.4L10 2z" />
        <path d="M6.5 10h2l1-2 1.5 4 1-2h1.5" />
      </>
    ),
  },
  send: {
    viewBox: '0 0 16 16',
    strokeWidth: 1.8,
    body: (
      <>
        <path d="M8 13V3" />
        <path d="M4 7l4-4 4 4" />
      </>
    ),
  },

  // --- drawn to match ------------------------------------------------------
  // The Knowledge rail entry. Drawn to sit beside `notes`, which is a sheet of
  // paper with a folded corner: this is a bound volume — one closed outline for
  // the covers, the spine down the middle, and two short rules on the right leaf
  // at the same 3-unit rhythm as the `notes` glyph's text lines. Nothing is
  // drawn on the left leaf: at 18px a second pair of rules closes the gap
  // between them and the spine, and the book reads as a filled box.
  book: {
    body: (
      <>
        <path d="M3.5 4.2h4.6A1.9 1.9 0 0 1 10 6.1v9.8a1.6 1.6 0 0 0-1.6-1.3H3.5z" />
        <path d="M16.5 4.2h-4.6A1.9 1.9 0 0 0 10 6.1v9.8a1.6 1.6 0 0 1 1.6-1.3h4.9z" />
        <path d="M12.3 8h2.4M12.3 11h2.4" />
      </>
    ),
  },
  star: {
    body: (
      <path d="M10 3l2.1 4.3 4.7.7-3.4 3.3.8 4.7L10 13.8 5.8 16l.8-4.7L3.2 8l4.7-.7L10 3z" />
    ),
  },
  starFilled: {
    filled: true,
    body: (
      <path d="M10 3l2.1 4.3 4.7.7-3.4 3.3.8 4.7L10 13.8 5.8 16l.8-4.7L3.2 8l4.7-.7L10 3z" />
    ),
  },
  dismiss: { body: <path d="M5.5 5.5l9 9M14.5 5.5l-9 9" /> },
  close: { body: <path d="M5.5 5.5l9 9M14.5 5.5l-9 9" /> },
  extract: { body: <path d="M4 6h12M4 10h12M4 14h7" /> },
  chevronRight: { body: <path d="M8 5l5 5-5 5" /> },
  chevronDown: { body: <path d="M5 8l5 5 5-5" /> },
  check: { body: <path d="M4.5 10.3l3.6 3.6 7.4-7.8" /> },
  warning: {
    body: (
      <>
        <path d="M10 3.2L17.4 16H2.6L10 3.2z" />
        <path d="M10 8.1v3.3M10 13.5v.6" />
      </>
    ),
  },
  spinner: {
    spin: true,
    body: (
      <>
        <circle cx="10" cy="10" r="7" strokeOpacity="0.25" />
        <path d="M17 10a7 7 0 0 0-7-7" />
      </>
    ),
  },
  copy: {
    body: (
      <>
        <rect x="7" y="7" width="9" height="9" rx="2" />
        <path d="M13 7V5.5A1.5 1.5 0 0 0 11.5 4h-6A1.5 1.5 0 0 0 4 5.5v6A1.5 1.5 0 0 0 5.5 13H7" />
      </>
    ),
  },
  download: {
    body: (
      <>
        <path d="M10 3.2v8.6" />
        <path d="M6.6 8.4L10 11.8l3.4-3.4" />
        <path d="M4 15.4h12" />
      </>
    ),
  },
  edit: {
    body: (
      <>
        <path d="M4 16.2h3.1l8.1-8.1-3.1-3.1L4 13.1v3.1z" />
        <path d="M11.4 5.7l3.1 3.1" />
      </>
    ),
  },
  trash: {
    body: (
      <>
        <path d="M4 5.9h12" />
        <path d="M8 5.9V4.6a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v1.3" />
        <path d="M5.6 5.9l.7 9.3a1.3 1.3 0 0 0 1.3 1.2h4.8a1.3 1.3 0 0 0 1.3-1.2l.7-9.3" />
      </>
    ),
  },
  plus: { body: <path d="M10 4.4v11.2M4.4 10h11.2" /> },
  archive: {
    body: (
      <>
        <rect x="3" y="3.8" width="14" height="3.6" rx="1.2" />
        <path d="M4.5 7.4v7.4a1.4 1.4 0 0 0 1.4 1.4h8.2a1.4 1.4 0 0 0 1.4-1.4V7.4" />
        <path d="M8.2 10.6h3.6" />
      </>
    ),
  },
  unarchive: {
    body: (
      <>
        <rect x="3" y="3.8" width="14" height="3.6" rx="1.2" />
        <path d="M4.5 7.4v7.4a1.4 1.4 0 0 0 1.4 1.4h8.2a1.4 1.4 0 0 0 1.4-1.4V7.4" />
        <path d="M10 14.2V9.8M8.2 11.6L10 9.8l1.8 1.8" />
      </>
    ),
  },
  refresh: {
    body: (
      <>
        <path d="M16.2 10a6.2 6.2 0 1 1-1.9-4.4" />
        <path d="M16.4 3.1v3.3h-3.3" />
      </>
    ),
  },
  externalLink: {
    body: (
      <>
        <path d="M11.2 3.8H16v4.8" />
        <path d="M16 3.8l-6.6 6.6" />
        <path d="M14.4 11.6V15a1.4 1.4 0 0 1-1.4 1.4H5a1.4 1.4 0 0 1-1.4-1.4V7a1.4 1.4 0 0 1 1.4-1.4h3.4" />
      </>
    ),
  },
  filter: { body: <path d="M3.4 4.8h13.2l-5.1 5.8v5.2l-3-1.7v-3.5L3.4 4.8z" /> },
  attach: {
    body: (
      <path d="M13.6 9.4l-4.7 4.7a2.7 2.7 0 0 1-3.8-3.8l5.7-5.7a1.8 1.8 0 0 1 2.6 2.6l-5.6 5.6a.9.9 0 0 1-1.3-1.3l5-5" />
    ),
  },
  stop: { body: <rect x="6" y="6" width="8" height="8" rx="1.6" /> },
  ellipsis: {
    filled: true,
    body: (
      <>
        <circle cx="4.6" cy="10" r="1.5" />
        <circle cx="10" cy="10" r="1.5" />
        <circle cx="15.4" cy="10" r="1.5" />
      </>
    ),
  },
} as const satisfies Record<string, Glyph>

export type IconName = keyof typeof GLYPHS

export interface IconProps {
  name: IconName
  /** Rendered box, in px. 18 is the rail/button default. */
  size?: number
  className?: string
  strokeWidth?: number
}

export default function Icon({ name, size = 18, className, strokeWidth }: IconProps) {
  const glyph: Glyph = GLYPHS[name]
  const spinning = glyph.spin === true
  const filled = glyph.filled === true

  return (
    <svg
      width={size}
      height={size}
      viewBox={glyph.viewBox ?? '0 0 20 20'}
      fill={filled ? 'currentColor' : 'none'}
      stroke={filled ? 'none' : 'currentColor'}
      strokeWidth={strokeWidth ?? glyph.strokeWidth ?? 1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      className={`${spinning ? 'animate-spin ' : ''}shrink-0${className ? ` ${className}` : ''}`}
    >
      {glyph.body}
    </svg>
  )
}
