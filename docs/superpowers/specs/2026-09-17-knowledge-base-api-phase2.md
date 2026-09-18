# Knowledge base — HTTP contract added in Phase 2 (`feat/kb-vectors`)

**Status: agreed before implementation (2026-09-18).** The frontend task (2.7) builds against this
file while the backend tasks (2.1–2.6) are still running. Where a backend task and this file
disagree at integration time, **this file is the contract and the deviation is reported**, not
silently absorbed; a deliberate change is made here, in the same PR. When Phase 2 merges, the code
and `backend/CLAUDE.md` become the reference and this file stays as the record of what was agreed.

Companion to the spec (`2026-09-17-knowledge-base-design.md`) and the plan
(`../plans/2026-09-17-knowledge-base.md`, see its Decisions log). The Phase 1 routes are documented
in `backend/CLAUDE.md`.

Conventions carried from Phase 1: all routes under `/api/kb` unless stated; JSON; datetimes are
**naive UTC** ISO strings (the frontend re-appends `Z` via `lib/dates.ts::parseUtc`); `detail` is
the error string on every non-2xx.

Every field is tagged with the backend task that produces it.

---

## 1. Changes to Phase 1 payloads

### `EntryRead` — two new fields *(2.5)*

```
EntryRead {
  …all Phase 1 fields unchanged…
  compile_model: string | null,           # 2.5 — the model that answered (fallbacks can switch it)
  compile_prompt_version: number | null,  # 2.5
}
```

`summary_md`, `compiled_at`, `possible_duplicate_of`, `chunks`, `pending_chunks`, `review_status`,
`authorship` **already exist** and need no change. In particular:

- **`possible_duplicate_of: number | null`** — Phase 1 field, **now actually populated** *(2.3)*.
  It is set at capture time from the first body chunk's vector plus the title trigram, on the
  **newer** entry, pointing at the older one. It is never set by a compile.
- **Per-entry embedding status** is **derived on the client**, not a new field: `"8 of 11 chunks
  embedded"` is `chunks - pending_chunks` of `chunks` *(counts produced by 2.1's embedder writing
  `embedded_at`; the counts themselves are Phase 1's `EntryFacts`)*.

### `EntryDetailRead` — unchanged

Still `EntryRead + {snapshot_md, versions[], activity[]}`.

### `HitRead` — **unchanged on the wire** *(2.2)*

```
Hit { entry: EntryRead, snippet: string, score: number, matched_by: 'keyword'|'vector'|'both'|'entity' }
```

- `matched_by` gains **real** `vector` and `both` values once a Voyage key is configured. The
  `Literal` and the client's `MatchedBy` union already contain all four — **no code change**.
- **`distance` is NOT added.** `Hit.distance` stays internal (the near-duplicate check and, in Phase
  3, the reranker read it). `score` remains **opaque** — no client may compare scores across calls,
  which is what lets Task 3.6 add a reranker without a contract change.
- The spec's "narrow filter — results may be incomplete" notice is **not on the wire in Phase 2**.
  Task 2.2 ships the signal as `SearchOutcome.topic_filter_truncated` internally; surfacing it would
  need `SearchResponse` (2.5's file) and the UI. Deferred to Phase 3 (plan decision P2-14).

### `SearchResponse.mode` *(2.1)*

`{hits: Hit[], mode: 'keyword' | 'hybrid'}` — already in the schema; `hybrid` now really happens,
because `KbService.embedder.dimensions > 0` once a Voyage key is configured
(`app/kb/service.py:357-360`). No shape change.

### `Stats` *(2.1)*

`embeddings_configured: bool` already exists and now flips to `true` with a key configured.
`pending_chunks` is the count the UI's "waiting for an embedding" line reads. No shape change.

### `POST /api/kb/embed-pending` → `200` *(2.1, plan decision P2-15)*

```
{ "embedded": 120, "pending": 35, "tokens": 48211 }
```

User-triggered (Settings → Knowledge → **Embed now**). Embeds up to a bounded number of pending
chunks per call and reports how many are still waiting, so the client calls it again while
`pending > 0` and the user has not stopped. `409 {detail}` when no Voyage key is configured;
`502 {detail}` when Voyage refuses (the chunks stay pending and the count says so). Writes one
`kb_activity` row (`action: 'embed'`) with the Voyage token count. Full re-index / rebuild is
Task 4.4.

### "Needs attention" — **no endpoint**, a client-side derivation *(2.7)*

There is no `GET /api/kb/attention`. The strip is computed in the client from data it already has:

| row | source | action |
|---|---|---|
| flagged duplicate | `EntryRead.possible_duplicate_of !== null` *(2.3)* | Merge |
| unreviewed model-authored | `authorship === 'model' && review_status === 'unreviewed'` *(2.6)* | Mark reviewed / Delete |
| capture or compile failure | `GET /api/kb/activity` row with `action === 'skip'` (capture) or a `compile` row whose `detail` names a refusal *(Phase 1 + 2.5)* | Retry / open |
| soft-deleted | the existing `?deleted=true` list *(Phase 1)* | Undo |

An ordinary captured article never produces a row. That rule is I16 and is the strip's headline test.

---

## 2. Settings (`GET` / `PUT /api/settings`) — all *(2.1)*

### New read-only fields on `SettingsRead`

```
has_voyage_key: boolean          # a key is stored IN THIS DATABASE — not "a key is usable"
voyage_api_key_masked: string    # mask_key() output, e.g. "…a1b2"; "" when nothing is stored
voyage_key_source: 'env'|'stored'|'none'   # mirrors key_source exactly
```

Precedence, mirroring the Anthropic key: **process environment `VOYAGE_API_KEY` → `.env` (i.e.
`Settings.voyage_api_key`) → the key stored in the DB.** Neither external value is ever written back
to the DB. `voyage_key_source === 'env'` with `has_voyage_key === false` is a normal, working state.

### New read/write fields (on both `SettingsRead` and `SettingsUpdate`)

| field | type | default | validation | read by |
|---|---|---|---|---|
| `kb_embedding_model` | string | `"voyage-4"` | 1–200 chars | 2.1 |
| `kb_capture_findings` | boolean | `false` | — | 2.6 |
| `kb_compile_mode` | `'manual'\|'auto'` | `"manual"` | closed set | 2.5 |
| `kb_compile_model` | string | `"claude-sonnet-5"` | 1–200 chars | 2.5 |
| `kb_compile_effort` | `'low'\|'medium'\|'high'\|'xhigh'\|'max'` | `"low"` | the existing `Effort` | 2.5 |
| `kb_compile_prompt` | string | the shipped default (returned in full, never empty) | ≤ 20 000 | 2.5 |
| `kb_compile_max_chars` | number | `24000` | 1 000–200 000 | 2.5 |
| `kb_compile_monthly_token_budget` | number | `5000000` | 0–1 000 000 000 | 2.5 |
| `kb_auto_accept_suggestions` | boolean | `true` | — | 2.5 |
| `kb_reviewed_only` | boolean | `false` | — | 2.2/2.5 |
| `kb_recency_boost` | boolean | `true` | — | 2.2 |
| `kb_rerank` | boolean | `true` | — | **nobody in Phase 2** (Task 3.6) |
| `kb_duplicate_threshold` | number (float) | `0.92` | 0.0–1.0 | 2.3 |

### New write-only field on `SettingsUpdate`

`voyage_api_key: string` — ≤ 500 chars, stripped on write, an empty string clears the stored key.
**Never returned by any endpoint.**

### Side effect of a `PUT` *(2.1)*

Changing `kb_embedding_model` to a **different** value, in the same transaction:
`DELETE FROM kb_chunk_vec` + `UPDATE kb_chunks SET embedded_at = NULL, embedding_model = NULL`, plus
a `kb_activity` row with `action: 'reindex'`. A `PUT` that re-sends the same model does nothing.
The response's `kb_schema_version` is unchanged (dimensions did not move).

---

## 3. Bulk capture *(2.3)*

### `POST /api/kb/bulk` → **SSE stream** (`text/event-stream`)

Request:

```json
{ "item_ids": [12, 13, 14], "job_id": "5f3c…" }
```

`item_ids` 1–200 feed-item ids; `job_id` optional (the server generates one — but the client should
send it, because Cancel needs it before the first frame arrives).

`409 {detail}` when a job with that key is already running (`kb:bulk:{job_id}`).
Headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no`, `ping` every 15 s.

Frames, in order:

| event | data | meaning |
|---|---|---|
| `turn_start` | `{"turn": 0}` | the job started |
| `text_delta` | `{"text": "<json>"}` where `<json>` is `{"item_id":int,"entry_id":int\|null,"created":bool,"skipped_reason":string\|null,"possible_duplicate_of":int\|null,"done":int,"total":int}` | **one per finished item, in completion order, not submission order** |
| `error` | `{"error_type":"cancelled","message":"The turn was stopped before it finished."}` | emitted by `pump_agent_events` when the job was cancelled or the client went away |
| `error` | `{"error_type":"api_error","message":"A turn is already running."}` | a duplicate key lost the race; the stream then ends |
| `done` | `{"saved":int,"skipped":int,"duplicates":int,"entry_ids":[int]}` | **terminal**, emitted after the pump finishes — including after a cancel, so the page can show what was saved |

The payload rides inside `text_delta` because the SSE vocabulary (`app/agent/events.py`) is the
agent package's and Phase 2 does not widen it. The client parses `JSON.parse(frame.text)`.

### `POST /api/kb/bulk/cancel`

```json
{ "job_id": "5f3c…" }    →    { "cancelled": true }
```

`200` with `cancelled: false` when nothing is running — the Stop button is allowed to lose the race.
Entries captured before the cancel **stay captured**.

---

## 4. Compile and the budget *(2.5)*

### `POST /api/kb/entries/{id}/compile` → `200 CompileResponse`

```
CompileResponse {
  entry: EntryRead,                 # refreshed, so summary_md / compiled_at / compile_model are current
  compiled: boolean,
  reason: string | null,            # only when compiled === false
  reason_code: 'budget'|'refusal'|'parse'|'no_text'|'api_error' | null,
  input_tokens: number,
  output_tokens: number,
  model: string | null,
  prompt_version: number | null,
  new_topic: { name: string, description: string | null } | null,   # proposed, NOT created
  suggested_topic_ids: number[],
  suggested_tags: string[],
  entities: [{ kind: string, value: string }]
}
```

- `404` unknown entry. **Everything else is a 200 with `compiled: false`** — a budget stop, a refusal
  and a malformed answer are outcomes, not server errors. Never a 500, never a 402.
- Suggestions are **already applied** when `kb_auto_accept_suggestions` is on, and the applied rows
  carry `suggested: true` in `EntryRead.topics[]` / `tags[]`. With the setting off they are reported
  here and not applied.
- `new_topic` **creates nothing**. The client confirms it with the existing
  `POST /api/kb/topics {name, description}` (201, or 409 when the name is taken).

### `POST /api/kb/compile` (batch)

Request `{ "entry_ids": [1,2,3] }` (1–100 ids).

- With **`?estimate=1`** → `200`, **no model call**:
  ```
  { "entries": 3, "input_tokens": 41230, "budget_remaining": 4958770, "would_exceed": false }
  ```
  `input_tokens` comes from `messages.count_tokens` on the prompt the batch would send.
- Without it → `200 { "results": CompileResponse[] }`, one per entry, in request order.

### `GET /api/kb/budget` → `200`

```
{ "month": "2026-09",                    # calendar month, UTC
  "limit": 5000000,                      # kb_compile_monthly_token_budget
  "anthropic_input": 120345,             # includes cache_creation_input_tokens + cache_read_input_tokens
  "anthropic_output": 8801,
  "anthropic_total": 129146,
  "remaining": 4870854,
  "exhausted": false,
  "voyage": 412000 }                     # month-to-date Voyage tokens, COUNTED SEPARATELY
```

Derived from `kb_activity`: Anthropic from `action IN ('compile','recompile')`, Voyage from
`action = 'embed'`. **The two counters are never added together.**

**Settings label (prescribed by I6 and the acceptance).** The budget control must say, in as many
words, that *this counts compile tokens only and does not include chat spend*, and link to the
per-session token counts the chat already shows. Task 2.5 hands Task 2.7 the exact sentence.

---

## 5. Findings *(2.6)*

No new endpoint. A finding is an ordinary entry, so it appears through the Phase 1 routes with:

```
kind: 'finding', authorship: 'model', review_status: 'unreviewed', captured_by: 'auto',
url: null, published_at: null, links.session_id: <the chat>, snapshot_md: "## Question … ## Answer … ## Sources …"
```

- `GET /api/kb/entries` (the Knowledge page, `search_for_user`) **shows it immediately**.
- `POST /api/kb/search` and the two chat tools (`search_for_model`) **never return it** until
  `review_status === 'reviewed'`, whatever `kb_reviewed_only` says. After review, the chat tool's
  rendered title carries `[AI finding, reviewed] ` — applied at read time by
  `app/agent/builtin.py::MODEL_TITLE_PREFIX`, **not stored in `title`**.
- Reviewing is the existing `PATCH /api/kb/entries/{id} {review_status: 'reviewed'}`; it now also
  rewrites the entry's vec0 metadata *(2.2)*, so the change takes effect on the vector leg too.
- With `kb_capture_findings` off (the default) nothing is written at all.

---

## 6. Endpoints that do NOT exist in Phase 2

Do not call these; they are later phases and there is no stub.

| endpoint | phase |
|---|---|
| `POST /api/kb/reindex`, `POST /api/kb/rebuild` (`embed-pending` above is the Phase 2 subset) | 4.4 |
| `POST /api/kb/digests` | 4.2 |
| `GET /api/kb/export.zip` | 4.3 |
| topic merge / rename-with-counts / `last_used_at` surfacing | 4.1 |
| `kb_entry_id` on inbox items, `kb` in global search | 3.5 |
| `search_result` blocks / `citations[]` in the chat | 3.1–3.3 |
| `kb_previously_covered` in notes generation | 3.4 |
| anything reading `kb_rerank` | 3.6 |

**Pending chunks in Phase 2** clear through `POST /api/kb/embed-pending` (and as entries are captured
or re-saved). There is no full Re-index button yet; the UI offers **Embed now**, not a control that 404s.

---

## 7. Frozen surface — unchanged by every Phase 2 task

The vec0 `CREATE` and its six metadata columns, the FTS5 tokenizer
(`unicode61 remove_diacritics 2`), the four triggers, `kb_chunks.id AUTOINCREMENT`, and the two chat
tools' **names, descriptions and ordering** (`fetch_article, get_feed_item, get_kb_entry,
search_feed_items, search_knowledge_base`) are frozen. Phase 2 changes what the tools *return*,
never how they are declared — the prompt-cache prefix moved once, in Phase 1, and does not move again.
