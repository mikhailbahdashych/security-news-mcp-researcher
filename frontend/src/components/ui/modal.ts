import { useEffect, useRef, type RefObject } from 'react'

/**
 * What Tab can land on. Deliberately a query rather than a walk of the DOM: an
 * overlay's panel is small, and anything outside it is what we are keeping the
 * keyboard away from.
 */
const FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),' +
  'textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'

/**
 * The keyboard contract every overlay owes.
 *
 * Focus moves into the panel on mount and back to whatever opened it on unmount,
 * Escape closes from anywhere on the page, and Tab cycles within the panel
 * rather than walking onto the page underneath. That last part is what
 * `aria-modal="true"` promises a screen-reader user has already happened — a
 * dialog that says it and does not is worse than one that never claimed to be
 * modal at all.
 *
 * Mount the panel conditionally: "open" is this hook's mount, which is also what
 * stops a closed overlay's queries running in the background.
 */
export function useModalPanel<T extends HTMLElement>(onClose: () => void): RefObject<T | null> {
  const panelRef = useRef<T | null>(null)

  // Focus in on mount, back out on unmount.
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const panel = panelRef.current
    ;(panel?.querySelector<HTMLElement>(FOCUSABLE) ?? panel)?.focus()
    return () => previous?.focus?.()
  }, [])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
        return
      }
      const panel = panelRef.current
      if (event.key !== 'Tab' || !panel) {
        return
      }
      const focusable = [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)]
      if (focusable.length === 0) {
        return
      }
      const edge = event.shiftKey ? focusable[0] : focusable[focusable.length - 1]
      // Focus outside the panel entirely (a click on the page behind, or a first
      // Tab after something stole it) comes back in rather than carrying on.
      if (document.activeElement === edge || !panel.contains(document.activeElement)) {
        event.preventDefault()
        ;(event.shiftKey ? focusable[focusable.length - 1] : focusable[0]).focus()
      }
    }

    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  return panelRef
}

/**
 * Is this the click that should dismiss an overlay?
 *
 * "Outside" is the whole decision behind a click-away dismissal, so it is a
 * function rather than a condition buried in a listener — and a pure one, which
 * is why it asks for the containers instead of reading refs itself.
 *
 * Two rules it would be easy to get wrong:
 *
 * - The **opener** counts as inside. Otherwise the button's own `pointerdown`
 *   closes the panel and its `click` immediately reopens it, and the toggle
 *   stops working.
 * - A missing target, or no containers at all, is **not** outside. Neither is
 *   evidence that the user clicked away, and guessing closes the panel under
 *   someone mid-action. A container that is still `null` (a ref before its
 *   element mounts) is skipped rather than counted as a miss.
 */
export function isOutside(target: Node | null, ...containers: (Element | null)[]): boolean {
  const mounted = containers.filter((container): container is Element => container !== null)
  if (!target || mounted.length === 0) {
    return false
  }
  return mounted.every((container) => !container.contains(target))
}
