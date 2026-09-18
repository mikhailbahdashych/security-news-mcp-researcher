import { Component, type ReactNode } from 'react'

import { type BoundaryState, nextBoundaryState } from './errorBoundaryState'

interface ErrorBoundaryProps {
  children: ReactNode
  /** Changing this clears a caught error **without** remounting the children. */
  resetKey?: string
}

/**
 * One panel's worth of blast radius.
 *
 * This is the only class component in the app, because catching a render error
 * is the one thing hooks cannot do. It exists for a specific failure: a payload
 * from a backend of a different version — a missing `index`, a `schema` that is
 * not there yet — is a `TypeError` inside one small panel, and without a
 * boundary React unmounts the **whole tree**, so a stats row nobody was looking
 * at takes the Inbox, the chat and the rail with it.
 *
 * Deliberately plain: no retry button (there is nothing to retry — the next
 * render throws the same way) and no error text on screen (React already logs
 * the error and the component stack to the console, and a stack trace in the
 * middle of Settings is not something the reader can act on).
 *
 * It does **not** reset itself. A caller whose content changes underneath it —
 * one entry to another — passes a `key`, which remounts it. A caller whose
 * children must **survive** that change passes `resetKey` instead: the routed
 * pane, where `/chat` → `/chat/12` is the same mounted page with a live turn in
 * its reducer, and `/knowledge/7` → `/knowledge` must come back to the filters
 * it left. A `key` there would throw both away on every click.
 */
export default class ErrorBoundary extends Component<ErrorBoundaryProps, BoundaryState> {
  state: BoundaryState = { failed: false, resetKey: this.props.resetKey }

  static getDerivedStateFromError(): Partial<BoundaryState> {
    return { failed: true }
  }

  static getDerivedStateFromProps(
    props: ErrorBoundaryProps,
    state: BoundaryState,
  ): BoundaryState | null {
    return nextBoundaryState(state, props.resetKey)
  }

  render() {
    if (this.state.failed) {
      return <p className="text-[11.5px] text-muted">This section failed to render.</p>
    }
    return this.props.children
  }
}
