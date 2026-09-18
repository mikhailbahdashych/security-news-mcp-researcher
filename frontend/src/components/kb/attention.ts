import { duplicateLabel, type KbActivity, type KbEntry } from '../../api/kb'

/**
 * The whole rule behind the "Needs attention" strip, in one tested function.
 *
 * There is **no endpoint** for this: the strip is derived from data the page
 * already holds (ruling I16). Four things can land in it, and nothing else:
 *
 * | row | where it comes from |
 * |---|---|
 * | flagged duplicate | `possible_duplicate_of !== null` on a live entry |
 * | unreviewed model-authored | `authorship === 'model' && review_status === 'unreviewed'` |
 * | capture / compile failure | a recent `kb_activity` row that records one |
 * | soft-deleted | the `?deleted=true` list, for Undo |
 *
 * **An ordinary captured article never produces a row.** That is the headline
 * test, and the reason the strip is worth having: with suggestions auto-accepted
 * there is nothing routine left to confirm, and a strip that lists every save is
 * a strip you learn to scroll past.
 */

export type AttentionKind = 'duplicate' | 'unreviewed' | 'failure' | 'deleted'

export interface AttentionRow {
  /** React key. Unique across the whole strip. */
  key: string
  kind: AttentionKind
  /** What the row's actions act on — `null` for a capture that made no entry. */
  entryId: number | null
  /** The entry a Merge folds this one into. The **older** of the two survives. */
  mergeInto: number | null
  /** The sentence the row shows. */
  text: string
  /** The trail row's timestamp, for a failure. */
  at: string | null
}

export interface AttentionOptions {
  /** The soft-deleted entries, newest first (`?deleted=true`). */
  deleted?: KbEntry[]
  /** Without a Voyage key a duplicate is flagged on the title alone — the row says so. */
  embeddingsConfigured?: boolean
  /** How many failures are worth showing. The trail is long; attention is not. */
  maxFailures?: number
}

/** Past this the trail is history, not a chore. */
export const ATTENTION_FAILURES = 5

/**
 * The failures the trail records, told apart from the successes in it.
 *
 * A compile's **success** is the only thing that writes JSON into `detail`:
 * `app/kb/compile.py` logs `json.dumps({"applied": …, "topic_ids": …})` on the
 * one path that stored a summary, and every other path goes through `_skip`,
 * whose `detail = detail or reason` is free prose. So JSON is the test.
 *
 * It used to be the other way round — three prose prefixes (`refused`,
 * `unusable answer`, `api error`) — and it missed two of the six outcomes,
 * including the first one a new user meets: `_skip(code="api_error")` with no
 * key configured writes the bare sentence "No Anthropic API key is configured.",
 * and `_skip(code="no_text")` writes "There is no text on this entry to
 * compile." Twelve failed compiles, and the one screen whose job is to say what
 * did not work said nothing. Prose drifts; the success format is the thing the
 * backend changes deliberately.
 */
const compiledSuccessfully = (detail: string | null): boolean =>
  (detail ?? '').trimStart().startsWith('{')

export function isFailure(row: KbActivity): boolean {
  if (row.action === 'skip' || row.action === 'budget_hit') {
    return true
  }
  if (row.action === 'compile' || row.action === 'recompile') {
    return !compiledSuccessfully(row.detail)
  }
  return false
}

function failureText(row: KbActivity): string {
  if (row.action === 'budget_hit') {
    return 'Not compiled — the monthly compile budget is spent.'
  }
  const what = row.action === 'skip' ? 'Capture failed' : 'Compile failed'
  const where = row.source ? ` (${row.source})` : ''
  return row.detail ? `${what}${where} — ${row.detail}` : `${what}${where}`
}

/**
 * The strip's rows, in a fixed order: duplicates, unreviewed model prose,
 * failures, then the bin.
 *
 * One row per entry at most, whichever leg found it first — an unreviewed
 * finding that is also a suspected duplicate is one chore, not two.
 */
export function needsAttention(
  entries: KbEntry[],
  activity: KbActivity[],
  options: AttentionOptions = {},
): AttentionRow[] {
  const { deleted = [], embeddingsConfigured = true, maxFailures = ATTENTION_FAILURES } = options
  const byId = new Map(entries.map((entry) => [entry.id, entry]))
  const seen = new Set<number>()
  const rows: AttentionRow[] = []

  const claim = (id: number | null): boolean => {
    if (id === null) {
      return true
    }
    if (seen.has(id)) {
      return false
    }
    seen.add(id)
    return true
  }

  // A deleted entry is in the bin; whatever else is true of it can wait for the
  // Undo. Claiming the ids first is what keeps it out of the other three legs.
  for (const entry of deleted) {
    seen.add(entry.id)
  }

  for (const entry of entries) {
    if (entry.deleted_at === null && entry.possible_duplicate_of !== null && claim(entry.id)) {
      rows.push({
        key: `duplicate-${entry.id}`,
        kind: 'duplicate',
        entryId: entry.id,
        mergeInto: entry.possible_duplicate_of,
        text: duplicateLabel(entry, byId.get(entry.possible_duplicate_of), embeddingsConfigured),
        at: null,
      })
    }
  }

  for (const entry of entries) {
    // `authorship === 'model'` and nothing else: an unreviewed *article* is the
    // resting state of every capture, and queueing those is the exact mistake
    // this strip exists not to make.
    if (
      entry.deleted_at === null &&
      entry.authorship === 'model' &&
      entry.review_status === 'unreviewed' &&
      claim(entry.id)
    ) {
      rows.push({
        key: `unreviewed-${entry.id}`,
        kind: 'unreviewed',
        entryId: entry.id,
        mergeInto: null,
        text: `Written by the model, not reviewed yet — ${entry.title}`,
        at: null,
      })
    }
  }

  let failures = 0
  for (const row of activity) {
    if (failures >= maxFailures) {
      break
    }
    if (!isFailure(row) || !claim(row.entry_id)) {
      continue
    }
    failures += 1
    rows.push({
      key: `failure-${row.id}`,
      kind: 'failure',
      entryId: row.entry_id,
      mergeInto: null,
      text: failureText(row),
      at: row.at,
    })
  }

  for (const entry of deleted) {
    rows.push({
      key: `deleted-${entry.id}`,
      kind: 'deleted',
      entryId: entry.id,
      mergeInto: null,
      text: `Deleted — ${entry.title}`,
      at: entry.deleted_at,
    })
  }

  return rows
}
