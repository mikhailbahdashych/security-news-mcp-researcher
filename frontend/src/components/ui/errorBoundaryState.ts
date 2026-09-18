export interface BoundaryState {
  failed: boolean
  /** The `resetKey` this state was derived under. */
  resetKey: string | undefined
}

/**
 * `getDerivedStateFromProps` for `ErrorBoundary`, as a pure function.
 *
 * A new `resetKey` is a fresh attempt: the caught error is cleared and the
 * children render again **in place** — nothing is remounted. `null` (React's
 * "no change") for the same key, which is also what keeps a boundary caught
 * on the re-render that follows `getDerivedStateFromError`.
 */
export function nextBoundaryState(
  state: BoundaryState,
  resetKey: string | undefined,
): BoundaryState | null {
  if (state.resetKey === resetKey) {
    return null
  }
  return { failed: false, resetKey }
}
