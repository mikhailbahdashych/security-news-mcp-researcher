/**
 * URL helpers.
 *
 * `hostOf` lived in `api/chat.ts` while the transcript's source cards were its
 * only caller. The knowledge base names its sources the same way, and a helper
 * with two callers belongs to neither module.
 */

/** The host of a URL, without `www.`. `null` when it is not a URL at all. */
export function hostOf(url: string | null | undefined): string | null {
  if (!url) {
    return null
  }
  try {
    return new URL(url).host.replace(/^www\./, '')
  } catch {
    return null
  }
}
