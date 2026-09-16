/**
 * An id for one note generation, in every context this app actually runs in.
 *
 * `crypto.randomUUID` exists only in a secure context, and "open it from the
 * other laptop" means `http://192.168.1.x:8000` — where the property is
 * undefined and calling it throws, taking the Generate click with it before a
 * single request is made.
 *
 * The fallback does not have to be a UUID. The id is a registry key: the server
 * uses it to match a Stop to a running generation, it is never stored, and it
 * only has to be unique among this browser's in-flight generations.
 */
export function generationId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  const random = Math.random().toString(36).slice(2, 12)
  return `gen-${Date.now().toString(36)}-${random}`
}
