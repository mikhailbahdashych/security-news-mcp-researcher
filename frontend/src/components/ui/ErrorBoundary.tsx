import { Component, type ReactNode } from 'react'

interface ErrorBoundaryProps {
  children: ReactNode
}

interface ErrorBoundaryState {
  failed: boolean
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
 * one entry to another — passes a `key`, which remounts it.
 */
export default class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { failed: false }

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { failed: true }
  }

  render() {
    if (this.state.failed) {
      return <p className="text-[11.5px] text-muted">This section failed to render.</p>
    }
    return this.props.children
  }
}
