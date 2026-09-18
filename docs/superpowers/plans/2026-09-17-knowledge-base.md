# Knowledge Base Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A durable, searchable knowledge base that grows from normal use of the app, feeds the chat and the notes, and survives the loss of the machine.

**Architecture:** An embedded RAG engine inside the app's SQLite file (`sqlite-vec` vectors + FTS5 keywords, fused), Voyage embeddings over HTTPS, LLM compile through a one-shot structured call, encrypted whole-database backups to S3. Everything behind `app/kb/service.py`.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2 async + aiosqlite / `sqlite-vec` / FTS5 / `httpx2` (Voyage) / `boto3` + `cryptography` (backups) / pytest with fakes — React 19 / TS / TanStack Query / vitest.

**Spec:** `docs/superpowers/specs/2026-09-17-knowledge-base-design.md` (**v2**, revised after the adversarial critique; the rulings it applies are `S1`–`S6` and `I1`–`I16`, listed in spec §8).

**Source of truth:** this plan, the spec and `docs/superpowers/specs/2026-09-17-knowledge-base-api-phase2.md` are the only authoritative planning documents, and they are tracked. The git-ignored `.superpowers/` directory is the executor's scratch (ledger, per-task briefs, reports, reviews, PR drafts): derived from these files, deleted when its work merges, never a second plan. Any decision that changes *what gets built* is written into the **Decisions log** at the end of this file, in the PR of the phase that made it — where the log and a task's text disagree, the log wins. Every phase branch is cut from `main` after the previous phase merged, and its PR base is `main`.

**How this plan is used:** it is the phase-and-task map. Each phase is one PR. When a phase starts, its tasks are expanded into step-level briefs (failing test → run → implement → run → commit, with the exact code) from this document and the spec, so code snippets never go stale across five phases. Every task below states its files, its interfaces, its tests, its acceptance check, and the ruling(s) it implements.

## Global Constraints

- No schedulers, no cron, no background polling. A capture, compile or backup runs only as the consequence of a user action. A **single** capture runs inside the request or turn that caused it; a **bulk** capture is a cancellable SSE job on `pump_agent_events` with the task-registry key `kb:bulk:{id}`, exactly like note generation (I2).
- No local models. Embeddings = Voyage API; summaries = Anthropic API.
- No network in tests: `FakeEmbedder`, the scripted Anthropic fake, `httpx2.MockTransport`, `moto[s3]`.
- `httpx2`, never `httpx`. Naive-UTC datetimes via `utcnow()`. New columns on existing tables go through `ADDED_COLUMNS`; **new indexes through `ADDED_INDEXES`**; new tables through `create_all`; the two virtual tables through **versioned** explicit DDL in `init_db` with `rebuild_vec` / `rebuild_fts` routines (I10).
- **The `kb_chunk_vec` column set is frozen in Phase 1.** vec0 has no `ALTER`; adding a metadata column later is a drop-and-rebuild of every vector. Every filter must be expressible in the six columns the spec names (S1).
- **Every process that opens the database loads `sqlite-vec`.** Without the extension `kb_chunk_vec` is an unknown module and `VACUUM` / `.dump` fail — this covers the engine's `on_connect` hook, the backup path, and anything documented for the CLI (I13).
- **Authorship is recorded on every entry and gates retrieval.** `search_knowledge_base` and the notes generator never return an entry with `authorship='model'` unless `review_status='reviewed'`, independently of `kb_reviewed_only`; compiled summaries are never returned as evidence at all (S5).
- **Retrieved text is data, never instruction.** Every `search_result` block carries the fixed wrapper header and is capped at 2 000 chars; the tool description and the system-prompt paragraph repeat it; retrieved text never enters the system prompt or a note template (S6, I3). Those three strings are prompt-cache prefix — byte-stable, no timestamps.
- **Citations and `output_config.format` are mutually exclusive** (a 400). No compile, digest or other structured call may enable citations (I5).
- The Voyage key is write-only over the API (masked read-back), same as the Anthropic key. `BACKUP_PASSPHRASE` lives only in the environment.
- Every LLM call goes through `app/agent/runner.run(persist=False, ...)` with the app's refusal/fallback handling — **with one documented exception**: `app/agent/oneshot.py::structured_call`, tool-free, persistence-free, for structured output only. No direct `messages.create` anywhere else (I5).
- Plain commit messages, explicit `git add`, no attribution trailers, never merge, never push to main.
- Frontend: tokens only, the `embedded` contract, no jsdom (pure logic in `.ts`).

---

## Phase 1 — A usable keyword knowledge base (PR `feat/kb-foundation`)

Outcome: **save an article from a starred item or a pasted URL, list it, keyword-search it, read and annotate it** — end to end, with no Voyage key, no Anthropic call and no hand-inserted row. Implements I15's re-cut: everything irreversible (the vec0 column set, the tokenizer, the time columns, authorship, soft delete) lands here; everything refinable comes later.

### Task 1.1: Prove the extension loads everywhere
- **Rulings:** I13, minor 12.
- **Files:** `backend/pyproject.toml` (+`sqlite-vec>=0.1.9,<0.2`), `backend/app/db/engine.py` (`on_connect` hook that loads `sqlite_vec`), `backend/tests/test_db.py`.
- **Interfaces:** every engine connection has `vec0` and FTS5 available; a helper that reports `vec_version()` and the FTS5 compile option for the Settings "index stats" panel.
- **Tests:** a connection can create a `vec0` table and run a KNN query; `PRAGMA compile_options` contains `ENABLE_FTS5`.
- **Acceptance:** pytest green on macOS; `docker build` then, inside the image, a one-liner printing **both** `vec_version()` and the FTS5 compile option, recorded in the PR body. If the base image's `sqlite3` lacks `enable_load_extension`, the task switches the runtime to a Python build that has it — decided and documented here, not later.

### Task 1.2: Schema, final and frozen
- **Rulings:** S1, S3, S4, S5, I1, I9, I11, I12, minors (`lang`, `possible_duplicate_of`, `kb_activity(at)`).
- **Files:** `backend/app/kb/__init__.py`, `backend/app/kb/models.py`, `backend/app/db/init.py` (virtual tables, FTS content-sync triggers, the vec delete trigger), `backend/app/db/models.py` (import so `create_all` sees the tables), `backend/tests/test_kb_schema.py`.
- **Interfaces:** ORM classes `KbEntry`, `KbSnapshot`, `KbChunk`, `KbEntryEntity`, `Topic`, `KbEntryTopic`, `KbEntryTag`, `KbEntryLink`, `KbActivity` exactly as spec §4.1 — including `authorship`, `lang`, `published_at`, `deleted_at`, `content_hash`, `source_ref`, `possible_duplicate_of`, `current_snapshot_id`, per-chunk `embedding_model`/`embedded_at`, and `kb_chunks.id INTEGER PRIMARY KEY AUTOINCREMENT`. Virtual-table DDL constants: `kb_chunks_fts` with `tokenize="unicode61 remove_diacritics 2"`, `kb_chunk_vec` with the six metadata columns + `embedding float[1024]`.
- **Tests:** tables exist after `init_db`; the partial unique indexes (url scoped to `deleted_at IS NULL`, note id, turn message id, feed item id) reject duplicates and accept NULLs; inserting a chunk populates the FTS row; **deleting a chunk deletes its vec row via the trigger**; deleting an entry cascades snapshots, chunks, FTS rows, vec rows, entities, topics and tags; `ondelete=SET NULL` on `feed_item_id`/`note_id`/`session_id`/`turn_message_id` leaves the entry alive with `source_ref` intact; a chunk id is never reused after a delete (AUTOINCREMENT).
- **Acceptance:** `init_db` twice is idempotent; the vec0 `CREATE` statement in the PR body is the one the spec prints, because it can never be altered again.

### Task 1.3: Schema-evolution machinery
- **Rulings:** I10 (lands in the same PR as 1.2, deliberately not a Phase 4 discovery).
- **Files:** `backend/app/db/init.py` (`ADDED_INDEXES`, `_ensure_indexes`, version check), `backend/app/kb/schema.py` (`VEC_DDL_VERSION`, `FTS_DDL_VERSION`, `rebuild_vec(dimensions)`, `rebuild_fts()`), `backend/app/services/settings.py` (`kb_schema_version`), `backend/CLAUDE.md` ("How to add … a new index" and "… a change to a virtual table"), root `CLAUDE.md` (the escaping-rule line, see 1.5).
- **Interfaces:** `ADDED_INDEXES: dict[str, dict[str, str]]` of `CREATE INDEX IF NOT EXISTS` DDL, run by `init_db` after `ADDED_COLUMNS`; `kb_schema_version` stores `{version, vec_ddl_version, vec_dimensions, fts_ddl_version, tokenizer}`; `index_status(session) -> {"outdated": bool, "reasons": [...]}` for Settings.
- **Tests:** an index missing from a pre-existing database is created by `init_db` and creating it twice is a no-op; a stored `vec_ddl_version` behind the constant reports "outdated" and **does not** rebuild by itself; `rebuild_vec` builds under a second name, fills, swaps and drops (never drops first) and leaves unembedded chunks pending; `rebuild_fts()` restores a deliberately corrupted FTS index.
- **Acceptance:** an upgrade path test — build a database with the Phase-1-minus-indexes schema, run `init_db`, assert the final shape equals a fresh one.

### Task 1.4: Chunking
- **Rulings:** minor 4 (`chars/3.6`).
- **Files:** `backend/app/kb/chunking.py`, `backend/tests/test_kb_chunking.py`.
- **Interfaces:** `split_markdown(text: str, *, target_tokens=800, overlap_tokens=80) -> list[Chunk]` with `Chunk(ord, text, token_estimate)`; `estimate_tokens(text) -> int` = `ceil(len(text) / 3.6)`.
- **Tests:** empty → []; short text → one chunk; paragraphs packed to the target; a fenced code block is never split; overlap carries the tail of the previous chunk; headings start a new chunk; the estimate is the one `kb_compile_max_chars` is compared against.
- **Acceptance:** pure, no I/O.

### Task 1.5: `fts_query` and the keyword store
- **Rulings:** S2, I7 (the per-leg collapse), I13 (the benchmark).
- **Files:** `backend/app/kb/fts.py`, `backend/app/kb/store.py`, `backend/app/kb/retrieval.py`, `backend/app/kb/entities.py`, `backend/tests/test_kb_fts.py`, `backend/tests/test_kb_store.py`, `backend/tests/test_kb_retrieval.py`, root `CLAUDE.md` (amend "the single escaping rule": `LIKE` uses `matches`/`escape_like`, FTS5 uses `fts_query`).
- **Interfaces:** `fts_query(user_text: str) -> str` — split on whitespace, drop bare FTS operators (`AND OR NOT NEAR ^ * : ( )`), double embedded `"`, wrap every term as a quoted phrase, join with `AND`; the caller retries with `OR` when `AND` returns nothing. `KnowledgeStore` protocol: `upsert_vectors(rows)`, `delete_vectors(chunk_ids)`, `knn(query_vec, k, *, filters) -> [(chunk_id, distance)]`, `keyword(match, k, *, filters) -> [(chunk_id, bm25)]`, `entities(kind, value) -> [entry_id]`, `rebuild(dimensions)`; `SqliteKnowledgeStore(session_factory)`; `hybrid_search(...) -> list[Hit]` with the full signature from spec §4.4 (the vector leg is a no-op with `NullEmbedder`); `collapse_best_per_entry(rows)` and `rrf(rankings, k=60)`, both pure; `extract_entities(text) -> [(kind, value)]` with `CVE-\d{4}-\d{4,}`.
- **Tests:** `fts_query` on `CVE-2024-3094`, `log4j_rce`, a title containing `"`, `foo AND bar`, `foo*`, `^foo`, `""`, and the AND→OR fallback — each asserted as an exact `MATCH` string **and** executed against a real FTS5 table without raising; a KB containing `CVE-2024-3094` is found by typing it; per-leg collapse keeps one chunk per entry so a 13-chunk entry cannot occupy 13 slots; RRF ties and one empty leg; `NullEmbedder` → `matched_by='keyword'`; the exact-entity leg is prepended ahead of both legs.
- **Acceptance:** the keyword leg over **20 000** synthetic chunks, with the milliseconds recorded in the PR body and copied into spec §9. ("200 chunks under 100 ms" measured nothing.)

### Task 1.6: Capture (single), snapshots, entities, soft delete
- **Rulings:** S4, S5 (authorship), I1 (content hash), I11, I12.
- **Files:** `backend/app/kb/capture.py`, `backend/app/kb/urls.py` (`canonical_url`), `backend/app/kb/service.py`, `backend/tests/test_kb_capture.py`.
- **Interfaces:** `capture_article(sf, embedder, *, url, title, source_name, text, published_at, feed_item_id, captured_by) -> CaptureResult`; `capture_note(sf, embedder, note_id)`; `capture_url(sf, embedder, url)` (through `fetch_article` + `extract_article`); `refresh_snapshot(sf, embedder, entry_id) -> RefreshResult`; `soft_delete(sf, entry_id)` / `undelete(sf, embedder, entry_id)` / `purge(sf, ids)`; `merge_entries(sf, keep_id, drop_id)`; `content_hash(text) -> str`; `CaptureResult(entry_id, created, skipped_reason, possible_duplicate_of)`.
- **Tests:** each kind creates entry + snapshot v1 + chunks + FTS rows + entities; `published_at` comes from the feed item, else the extractor, else NULL, and **never** the capture time; dedup by URL, by feed item id and by `content_hash` adds a back-link and no entry; minimum-length skip writes an activity row; refresh with identical text inserts no version, changed text inserts v2, moves `current_snapshot_id` and re-chunks so search returns only the new text; soft delete drops the chunks and hides the entry from search, undelete re-chunks from the current snapshot; merge keeps the **older** entry, unions links/topics/tags/entities, concatenates notes and soft-deletes the newer; a capture never holds a transaction across an embedder call.
- **Acceptance:** capture of a 40 000-char article, then a keyword search for a phrase in its last paragraph, returns it.

### Task 1.7: Capture triggers, policy and the KB API
- **Rulings:** S5 (findings are absent here by construction), I11, I16.
- **Files:** `backend/app/api/items.py` (star → capture when `kb_capture_starred`), `backend/app/api/notes.py` / `services/notes.py` (save/generate → capture when `kb_capture_notes`), `backend/app/api/kb.py` (`POST /api/kb/entries` from an item id or a URL, entries list/detail/patch, `POST /entries/{id}/delete` + `/undelete`, `POST /entries/{id}/refresh`, `POST /entries/{id}/review`, `POST /entries/{id}/merge`, `POST /api/kb/purge`, topics CRUD, `POST /api/kb/search`, `GET /api/kb/stats`, `GET /api/kb/activity`), `backend/app/schemas/kb.py`, `backend/app/main.py` (router), `backend/app/services/settings.py` (`kb_capture_starred`, `kb_capture_notes`, `kb_min_snapshot_chars`, `kb_schema_version`).
- **Interfaces:** `KbService` is the only thing the API and (later) the agent tools call; every capture trigger is a call into it from the route that already committed the user's write, wrapped so its failure is an activity row rather than a 500.
- **Tests:** each trigger through the API with fakes; policy off → no entry; a capture error never fails the original request (the star still lands); list filters (topic, kind, entity, since, review, deleted); patch notes; delete → undelete round trip; the activity log is pruned above 10 000 rows.
- **Acceptance:** API contract tests green; `GET /api/kb/stats` reports entries, chunks, pending chunks and the index-format status.

### Task 1.8: The Knowledge page (minimal) and Settings
- **Rulings:** I15, I16.
- **Files:** `frontend/src/pages/Knowledge.tsx`, `frontend/src/components/kb/{EntryList,EntryDetail,SearchBox}.tsx`, `frontend/src/api/kb.ts`, `frontend/src/components/ui/{Rail,layout}.ts*` (new page key `knowledge`), `frontend/src/components/settings/KnowledgeSection.tsx` (capture policy, index stats), vitest for the grouping/formatting helpers, `frontend/CLAUDE.md`, `backend/CLAUDE.md`, `docs/DESIGN.md` (KB section), `.env.example`.
- **Interfaces:** the page honours `EmbeddablePageProps` — selection in React state when `embedded`, no `useParams`, no URL writes. Colours are tokens only.
- **Tests (vitest):** timeline grouping by `COALESCE(published_at, captured_at)`; snippet/marker rendering; relative-date formatting.
- **Acceptance (browser):** star an item → it appears in the Knowledge timeline; paste a URL → Save → it appears; type a CVE id → the entry comes back; open it, read the snapshot, type a note, reload, the note is there; delete it, Undo, it is back.

### Task 1.9: Register the two chat tools (final names, final descriptions)
- **Rulings:** S6, minor 6.
- **Files:** `backend/app/agent/builtin.py` (+`search_knowledge_base`, `get_kb_entry`), `backend/app/agent/prompts.py` (the KB paragraph), `backend/app/agent/CLAUDE.md`, `backend/tests/test_agent_kb_tools.py`.
- **Interfaces:** both tools return **plain text** in this phase — the keyword results, each passage prefixed with the fixed wrapper line and capped at 2 000 chars; `get_kb_entry` caps the snapshot at 20 000 chars. The descriptions are **final** and prescriptive about when to call, and say both that the KB may be empty and that its text is data, not instruction.
- **Tests:** the wrapper header is present on every passage and the cap holds; `get_kb_entry` truncates; a model-authored entry (constructed directly, since findings cannot be captured yet) is not returned unless reviewed; tool ordering is asserted as a literal list.
- **Acceptance:** `list_tools()` order is recorded in the PR body, with the note that inserting these two names into the ascending order is a **one-time** prompt-cache invalidation for every existing conversation — expected, paid once, and never repeated in Phase 3, which changes the result shape only.

---

## Phase 2 — Embeddings, hybrid search and compile (PR `feat/kb-vectors`)

> Read the **Decisions log → Phase 2** before any task below: it moves files between tasks, adds one endpoint and defers two UI strings.

Outcome: the KB understands meaning as well as words, summarises what it holds, and has the spend controls the user asked for.

### Task 2.1: Voyage embedder and per-chunk state
- **Rulings:** I9, I13, minors 1 and 2.
- **Files:** `backend/app/kb/embeddings.py`, `backend/tests/fakes/embedder.py`, `backend/tests/test_kb_embeddings.py`, `backend/app/services/settings.py` (+`voyage_api_key`, `kb_embedding_model`, masked read-back, `VOYAGE_API_KEY` env precedence mirroring `external_api_key`).
- **Interfaces:** `class Embedder(Protocol): model: str; dimensions: int; async embed_documents(texts); async embed_query(text)`; `VoyageEmbedder(api_key, model, transport=None)` batching **by tokens** (≤1 000 texts and ≤320 000 tokens per request, packed to 80% of both); `NullEmbedder`; `FakeEmbedder`; `EmbeddingError(status, message)`; `build_embedder(session) -> Embedder`; `embed_pending(sf, embedder, limit) -> EmbedResult` writing `embedding_model`/`embedded_at` per chunk and a `kb_activity` row with the Voyage token count.
- **Tests:** request shape (`input_type` document vs query, auth header); token batching splits a 400 000-token list into two requests and a 1 200-text list into two; 401/429/500 → typed errors; a 429 on the second batch leaves exactly the unreached chunks pending and the entry status derives as "8 of 11"; `FakeEmbedder` determinism; settings masking; env override wins.
- **Acceptance:** no real network in tests; the rejected alternatives (`int8`, `output_dimension: 512`, `voyage-context-4`) are recorded in the PR body as decided-and-closed.

### Task 2.2: vec0 store, hybrid retrieval, benchmark
- **Rulings:** S1, S5, I7, I13.
- **Files:** `backend/app/kb/store.py`, `backend/app/kb/retrieval.py`, `backend/tests/test_kb_store.py`, `backend/tests/test_kb_retrieval.py`.
- **Interfaces:** `upsert_vectors` writes all six metadata columns (`entry_id`, `entry_kind`, `chunk_kind`, `reviewed`, `authorship`, `published_day`); `knn(query_vec, k, *, filters)` puts **every** filter inside the `MATCH` (`= != < >` only); the model-authorship gate is **two KNN queries** merged by distance, and the second is skipped when the KB holds no model-authored entries; the topic filter uses an adaptive `k` doubling from 50 to a cap of 512; `Hit` carries the raw distance; `hybrid_search` collapses per leg, fuses with RRF over entry ids, then applies the recency prior (`kb_recency_boost`).
- **Tests:** KNN order with `FakeEmbedder`; **a filtered query with small `k` returns rows a post-filter would have lost** (the S1 regression); `reviewed`, `entry_kind`, `chunk_kind` and `published_day` each filter inside the KNN; the two-leg authorship gate returns a reviewed model entry and never an unreviewed one; the adaptive `k` stops at the cap and the caller is told; `matched_by` is `both` when the legs agree; a summary chunk never reaches an evidence result; the recency prior reorders two otherwise-equal hits and is off when the setting is off.
- **Acceptance:** KNN over **20 000** synthetic 1024-dim chunks, milliseconds recorded in the PR body and copied into spec §9 as the measured ceiling.

### Task 2.3: Near-duplicates and the bulk capture job
- **Rulings:** I1, I2.
- **Files:** `backend/app/kb/capture.py` (`near_duplicate`), `backend/app/kb/bulk.py`, `backend/app/api/kb.py` (`POST /api/kb/bulk` returning an SSE stream), `backend/tests/test_kb_bulk.py`.
- **Interfaces:** `near_duplicate(store, *, first_body_vector, title, threshold) -> entry_id | None` requiring cosine ≥ `kb_duplicate_threshold` **and** title trigram ≥ 0.8; `run_bulk_capture(sf, embedder, ids) -> AsyncIterator[AgentEvent]` on `pump_agent_events` with the registry key `kb:bulk:{id}`, `MAX_KB_EXTRACTIONS = 8`, progress and terminal frames documented.
- **Tests:** the near-duplicate check fires on the **first body chunk** at capture time (i.e. before any compile) and sets `possible_duplicate_of`; a high cosine with a dissimilar title does **not** flag; with `NullEmbedder` the trigram test runs alone and only flags; the bulk job emits progress, respects the concurrency cap, cancels mid-run leaving committed entries committed, and a second job for the same key gets the registry's error frame.
- **Acceptance (browser):** select 20 inbox items → bulk Save → progress, then Cancel halfway → the page shows what was saved.

### Task 2.4: `oneshot.structured_call`
- **Rulings:** I5.
- **Files:** `backend/app/agent/oneshot.py`, `backend/tests/test_oneshot.py`, `backend/app/agent/CLAUDE.md`.
- **Interfaces:** `async def structured_call(client, *, model, effort, system, user, schema) -> dict` — `client.beta.messages` with `betas=[FALLBACK_BETA]`, `fallbacks=FALLBACKS`, `output_config={"effort": effort, "format": …}`, no tools, no persistence, **no `cache_control`**, and the runner's `stop_reason` handling (content is read only after `stop_reason` is inspected).
- **Tests:** a scripted refusal raises the typed refusal error with its category and never touches `content`; a fallback response is handled; malformed JSON raises a typed parse error; `cache_control` is absent from the request; `max_tokens` and betas match the runner's constants.
- **Acceptance:** `agent/CLAUDE.md` records (a) that this is the one exception to "everything through `runner.run`" and why, and (b) that **citations + `output_config.format` is a 400**, so no structured call may enable citations.

### Task 2.5: Compile, its controls and the budget
- **Rulings:** I5, I6, I12, I16.
- **Files:** `backend/app/kb/compile.py`, `backend/app/kb/prompts.py` (default prompt, `COMPILE_PROMPT_VERSION`), `backend/app/api/kb.py` (`POST /entries/{id}/compile`, `POST /api/kb/compile` batch with `?estimate=1`, `GET /api/kb/budget`), settings `kb_compile_mode|model|effort|prompt`, `kb_compile_monthly_token_budget`, `kb_auto_accept_suggestions`, `kb_reviewed_only`, `kb_recency_boost`, `kb_rerank`, `kb_duplicate_threshold`, `backend/tests/test_kb_compile.py`.
- **Interfaces:** `compile_entry(sf, client_factory, entry_id) -> CompileResult`; `month_usage(sf) -> {anthropic_input, anthropic_output, voyage}` derived from `kb_activity` and **including `cache_creation_input_tokens` + `cache_read_input_tokens`**; `budget_allows(sf, estimate) -> bool`. The compile schema also returns `entities` (vendors/products) stored with `source='model'`.
- **Tests:** structured output parsed and stored with the prompt version; suggestions auto-accepted when `kb_auto_accept_suggestions` is on and still marked `suggested`, and left pending when it is off; unknown topic ids dropped; `new_topic` creates nothing until confirmed; budget hit → `budget_hit` activity and **no call**; refusal path; `auto` mode compiles at capture and `manual` does not; the Voyage counter is separate from the Anthropic one; no compile request carries citations.
- **Acceptance:** the Settings budget label states in as many words that this does not include chat spend, and links to the per-session counts.

### Task 2.6: Findings capture, off by default
- **Rulings:** S5.
- **Files:** `backend/app/agent/turns.py` (turn end → `capture_finding`), `backend/app/kb/capture.py`, settings `kb_capture_findings` (default **off**), `backend/tests/test_kb_findings.py`.
- **Interfaces:** `capture_finding(sf, embedder, *, session_id, turn_message_id, question, answer, sources)` writing `authorship='model'`, `review_status='unreviewed'`. "Cited ≥1 source" is **`services/notes.py::SourceCollector` semantics** — a successful `fetch_article` URL, or a `web_search` result whose URL appears in the finished text — not a regex for `http`.
- **Tests:** default off → no entry, even for a perfect turn; on → an entry with `authorship='model'` that `hybrid_search` refuses to return until reviewed, and returns afterwards with the `[AI finding, reviewed]` title prefix; a stopped turn → no finding; an answer that merely mentions a URL → no finding.
- **Acceptance:** with the setting off (the default), a full chat turn leaves `kb_entries` unchanged.

### Task 2.7: Hybrid search UI, "Needs attention", compile controls
- **Rulings:** I16, I6.
- **Files:** `frontend/src/pages/Knowledge.tsx`, `frontend/src/components/kb/{TopicSidebar,NeedsAttention,CompileDialog,EntryDetail}.tsx`, `frontend/src/components/settings/KnowledgeSection.tsx` (Voyage key, embedding model, compile mode/model/effort/prompt, budget + month-to-date + Voyage counter, Re-index, activity log), `frontend/src/api/kb.ts`, vitest.
- **Interfaces:** the strip is named **"Needs attention"** and lists only flagged duplicates, capture/compile failures and unreviewed **model-authored** entries — never a queue over everything captured.
- **Tests (vitest):** the strip is empty for a KB of ordinary captured articles; budget and token formatting; marker rendering for `vector` / `keyword` / `both` / `exact`.
- **Acceptance (browser):** enter a Voyage key → Re-index → a semantic query with no shared words finds the entry; "Compile N" shows an estimate then compiles; suggested topics are already applied and still marked suggested; the budget counter moves and the Voyage counter moves separately.

---

## Phase 3 — Integration (PR `feat/kb-integration`)

Outcome: the chat and the notes use the KB; the inbox and global search know about it.

### Task 3.1: Block-shaped tool results in the registry
- **Rulings:** I4.
- **Files:** `backend/app/agent/registry.py` (`ToolResult.content: str | list[dict]`), `backend/app/agent/runner.py` (block assembly + the all-or-nothing rule), `backend/app/agent/events.py` (preview), `backend/app/agent/persistence.py`, `frontend/src/components/chat/ToolCard.tsx`, `backend/app/agent/CLAUDE.md`, `backend/tests/test_registry.py`, `backend/tests/test_runner_blocks.py`.
- **Interfaces:** a list `content` is persisted **verbatim** into `messages.content_json` and `tool_calls.result_json`; the SSE `tool_result` preview stays a ≤600-char **string** (a text summary of the blocks); the runner raises nothing but normalises a mixed list — if any block is a `search_result`, all must be, so a partial list is an `is_error` result rather than a 400 from the API.
- **Tests:** a str result behaves exactly as today (no transcript change); a block list round-trips through persistence byte-for-byte and replays; the preview shaping; a mixed list is rejected before the request; the tool card renders `search_result` titles.
- **Acceptance:** the whole existing agent test suite is green unchanged — this is a widening, not a change.

### Task 3.2: KB tool results as `search_result` blocks
- **Rulings:** S5, S6, I4.
- **Files:** `backend/app/agent/builtin.py`, `backend/app/kb/service.py`, `backend/tests/test_agent_kb_tools.py`.
- **Interfaces:** each hit becomes `{"type":"search_result","source":"kb://entry/{id}","title":…,"content":[{"type":"text","text":…}],"citations":{"enabled":true}}`; the text still begins with the Phase-1 wrapper line and is still capped at 2 000 chars; **"the KB had nothing" is a pure text result**; a reviewed model-authored entry's title is prefixed `[AI finding, reviewed]`; summary chunks are never content. **The tool descriptions from Task 1.9 are not touched.**
- **Tests:** block shape against the documented schema; the empty case is pure text; the scripted fake accepts the list; blocks survive persistence verbatim; the wrapper and the cap; the authorship rules.
- **Acceptance:** a diff showing `prompts.py` and the tool descriptions unchanged since Phase 1 (the cache prefix moved once, in Phase 1).

### Task 3.3: Citations in the frontend
- **Rulings:** I14.
- **Files:** `frontend/src/api/chat.ts` (read `citations[]` off text blocks, `kb://` → Knowledge link), `frontend/src/lib/liveTurn.ts`, `frontend/src/components/chat/{SourcesGrid,Citations}.tsx`, vitest, `backend/CLAUDE.md` (the SSE event table, if the payload changes).
- **Interfaces:** `citationsFor(block) -> Citation[]` beside the existing `citationFor(href)`, which stays — the two provenance paths coexist; `kbLink(source)` maps `kb://entry/{id}`; the stored `content_json` is the source of truth on reload and is never rewritten.
- **Tests (vitest):** `citations[]` read from a **stored** transcript and from a live stream (`content_block_start` + `text_delta`); numbered references render in order and de-duplicate; `kb://entry/{id}` maps to the Knowledge page; http(s) citations feed the existing SOURCES grid; an answer with citations **and** no inline markdown URL still shows sources.
- **Acceptance (browser):** ask "have we covered Okta before?" with an Okta entry present → the answer cites it, the reference chip opens the entry, and the SOURCES grid is not empty.

### Task 3.4: Notes "Previously covered" as a user block
- **Rulings:** I3, I12.
- **Files:** `backend/app/services/notes.py` (search per item, the user-message block), `backend/app/services/settings.py` (`kb_previously_covered`, default on), `backend/tests/test_notes_generate.py`.
- **Interfaces:** hits are appended to the **user** message after the items, headed `Prior coverage from the knowledge base (titles, dates, summaries):`; the per-item search consults `kb_entry_entities` (CVE ids) before the text legs; **no `{{previously_covered}}` template slot exists**.
- **Tests:** the block is in the user content and `system_override` is byte-identical to a run without it; a **customised** template still gets the section; the setting off → absent; only entries older than the current items appear; a model-authored entry never appears unless reviewed.
- **Acceptance:** generate notes for items that duplicate an older KB entry → the section names it.

### Task 3.5: Inbox badge, single-row save, global search
- **Rulings:** I2, minor 7.
- **Files:** `backend/app/api/items.py` (`kb_entry_id` on items), `backend/app/services/search.py` (+`kb` entity), `backend/CLAUDE.md` (the response now has **four** keys), `frontend/src/components/inbox/**` (badge and the single-row "Save to knowledge base"; the **bulk** action ships in Task 2.7 — decision P2-4), `frontend/src/components/ui/GlobalSearch.tsx`.
- **Interfaces:** `SEARCH_TYPES = ("items", "sessions", "notes", "kb")` — the documented "always all three keys" contract becomes four, in the code, the tests and `backend/CLAUDE.md`, in this task and not by accident later. The single row action stays inline; the bulk action already exists (Task 2.7) and gains only the dedup report below.
- **Tests:** items carry the id; global search returns kb hits and the contract test asserts four keys; the bulk action dedups and reports what it skipped.
- **Acceptance (browser):** a saved item shows the badge and the badge deep-links to the entry.

### Task 3.6: Reranking
- **Rulings:** I7, I13.
- **Files:** `backend/app/kb/rerank.py`, `backend/app/kb/retrieval.py`, settings `kb_rerank`, `backend/tests/test_kb_rerank.py`.
- **Interfaces:** `Reranker` protocol beside `Embedder`; `VoyageReranker` calling `rerank-2.5` once over the fused top 30; `NullReranker` when there is no key or the setting is off. `Hit.score` stays **opaque** — no caller compares scores across calls.
- **Tests:** the request shape via `MockTransport`; the fused order is replaced by the reranked order; no key → no call and no error; the setting off → no call; a rerank failure degrades to the fused order and writes an activity row.
- **Acceptance:** a query whose best answer is ranked 7th by fusion is 1st after reranking, in a fixture.

---

## Phase 4 — Curation, export and index maintenance (PR `feat/kb-curation`)

### Task 4.1: Topics management
- **Rulings:** I16.
- **Files:** `backend/app/api/kb.py` (topic rename/describe/colour/merge, counts), `frontend/src/components/kb/TopicSidebar.tsx`, `backend/tests/test_kb_topics.py`.
- **Interfaces:** `merge_topics(sf, keep_id, drop_id)`; the sidebar shows per-topic counts, `last_used_at`, and an "unused for 12 months" marker so a year-3 taxonomy can be pruned instead of accumulating.
- **Tests:** merging re-points `kb_entry_topics` without creating duplicate rows; the unused marker is computed from `last_used_at`, which capture and compile both touch.
- **Acceptance (browser):** a taxonomy of 60 topics is still navigable, and the dead ones are visibly dead.

### Task 4.2: Digests
- **Rulings:** I5.
- **Files:** `backend/app/api/kb.py` (`POST /api/kb/digests`), `backend/app/services/notes.py` (digest template setting), `frontend/src/components/kb/TopicSidebar.tsx` (a "Digest" button on a topic), `backend/tests/test_kb_digests.py`.
- **Interfaces:** a digest is the notes generator over a topic + `since`, saved as a note linked to the topic. **Citations stay enabled and `output_config.format` is never used** — a digest is prose, not structured output, and the two together are a 400.
- **Tests:** the digest request carries no `format`; the saved note links back to the topic; an empty topic produces a message, not an empty note.
- **Acceptance (browser):** a topic with 12 entries produces a readable digest note.

### Task 4.3: Obsidian export
- **Rulings:** I13.
- **Files:** `backend/app/api/kb.py` (`GET /api/kb/export.zip`, per-topic variant), `backend/app/kb/export.py`, `backend/tests/test_kb_export.py`.
- **Interfaces:** one `.md` per entry with YAML frontmatter (title, url, `published_at`, `captured_at`, authorship, topics, tags, entities) plus the current snapshot, the summary and the user's notes. Vectors are **not** exported and do not need to be — they are the derivable half; this is the exit path for the half that is not.
- **Tests:** the zip round-trips; frontmatter keys are stable across runs; a per-topic export contains exactly that topic's entries; soft-deleted entries are excluded.
- **Acceptance:** the exported folder opens as an Obsidian vault with working links.

### Task 4.4: Re-index and rebuild
- **Rulings:** I9, I10.
- **Files:** `backend/app/api/kb.py` (`POST /api/kb/reindex`, `POST /api/kb/rebuild`), `backend/app/kb/schema.py`, `frontend/src/components/settings/KnowledgeSection.tsx` (the "index format outdated" banner and its button), `backend/tests/test_kb_reindex.py`.
- **Interfaces:** re-index embeds pending chunks, resuming from per-chunk state; rebuild runs `rebuild_vec(dimensions)` / `rebuild_fts()` and is offered **only** when `index_status()` reports outdated. Both stream progress as SSE like note generation, cancellable, no polling.
- **Tests:** a cancelled re-index leaves the chunks it finished embedded and the rest pending, and resuming does not re-pay for them; a rebuild never drops the old table before the new one is filled; a dimension change rebuilds and marks everything pending; the banner disappears once the stored versions match the constants.
- **Acceptance (browser):** with a deliberately stale `kb_schema_version`, Settings shows the banner, the rebuild runs with progress, and search works throughout except during the swap.

---

## Phase 5 — Cloud replication (PR `feat/kb-backup`)

### Task 5.1: Snapshot pipeline
- **Rulings:** I8, I13, minor 11.
- **Files:** `backend/app/backup/snapshot.py`, `backend/app/backup/crypto.py` (AES-256-GCM, scrypt, versioned header), `backend/pyproject.toml` (+`boto3`, `cryptography`, `moto[s3]` as a dev dependency), `backend/tests/test_backup.py`.
- **Interfaces:** `take_backup(db_path, config) -> BackupInfo` — `VACUUM INTO` on a **dedicated synchronous `sqlite3` connection** (never the async engine; a VACUUM fails if the connection has an open transaction) into a path from `tempfile.mkdtemp()` that is guaranteed **not** to exist (not `NamedTemporaryFile`); then, unless `BACKUP_INCLUDE_VECTORS`, `DELETE FROM kb_chunk_vec` + `UPDATE kb_chunks SET embedded_at = NULL, embedding_model = NULL` + `VACUUM` on the copy; then gzip, then AES-256-GCM. Header: magic, format version, scrypt `n=2**15, r=8, p=1`, salt, 96-bit nonce. `list_backups(config)`; `prune(config, keep=BACKUP_KEEP default 10)`; `restore_latest(config, db_path) -> RestoreInfo`; `BackupConfig.from_env()`.
- **Tests:** round trip through `moto[s3]` and a temp dir; the restored copy has no vectors and all chunks pending; header parsing, including reading the scrypt parameters **from the file** rather than from the code; a wrong passphrase fails closed and leaves the existing database untouched; prune keeps N; `latest.json` sha verified; **a newer `schema_version` is refused**; `VACUUM INTO` succeeds while the async engine holds a writer.
- **Acceptance:** the size of a vectors-excluded backup versus an included one, on a KB of 500 entries, recorded in the PR body. If `moto[s3]` will not lock cleanly with hash-verified wheels, the fallback is a minimal in-memory client stub — decided in this task's brief, not deferred again.

### Task 5.2: API, triggers, Settings
- **Rulings:** I8.
- **Files:** `backend/app/backup/api.py` (`POST /api/backup/run`, `GET /api/backup/status`, `POST /api/backup/restore`), `backend/app/main.py` (**restore-on-empty runs before `create_db_engine`** — the "database path does not exist" check is evaluated exactly once, before anything can create the file; then the engine, then `init_db`), `backend/app/kb/capture.py` (the coalesced after-capture trigger), `frontend/src/components/settings/BackupSection.tsx`, `.env.example`, `README.md`, root `CLAUDE.md` (the "no longer strictly local-only" paragraph **and** the deliberate exception that `BACKUP_PASSPHRASE` and the AWS credentials live in the environment, and that a restored backup hands whoever holds the passphrase both API keys).
- **Interfaces:** `maybe_restore_on_empty(settings) -> RestoreInfo | None`, called from the lifespan **before** `create_db_engine`; `schedule_backup_after_capture(sf)` — a debounced task started by a capture event, never a timer that fires on its own, with a 30-minute window and a minimum-growth check.
- **Tests:** the lifespan ordering (a restored file is in place before the engine is built — asserted by a fake restore that writes a marker table); `kb_backup_after_capture` defaults **off**; when on, two captures 5 minutes apart produce **one** upload and the 30-minute window is respected, with the last event winning; a backup failure surfaces in status and never fails the capture.
- **Acceptance (documented in the PR):** fresh clone + `.env` with the bucket, passphrase and AWS variables → first start restores the database → the KB, chats, notes and keys are all there, and the Knowledge page reports N chunks pending until Re-index runs.

---

---

## Decisions log

Decisions taken while executing the plan that change what gets built, where it lives, or which
task owns it. Newest phase last. **Where this log and a task's text disagree, the log wins.**
Each entry: the decision — why — what it costs if wrong. Entry numbers match the executor's ledger;
a gap is an entry folded into a neighbour.

### Phase 1 (merged as #30) — carried into Phase 2

- **C1. A same-dimension embedding-model change must empty the vector table.** `kb_chunk_vec` has no
  `embedding_model` column (frozen DDL) and `rebuild_vec` carries vectors over when the dimension is
  unchanged, so switching model at 1024 dims would mix two vector spaces in one KNN. Changing
  `kb_embedding_model` therefore runs `DELETE FROM kb_chunk_vec` and marks every chunk pending, in
  the same transaction (Task 2.1). — Cost if wrong: silently meaningless similarity scores.
- **C2. vec0 metadata has no sync path.** The `reviewed` / `authorship` columns are written once, at
  upsert. Reviewing or un-reviewing an entry, and soft-delete / undelete, must update or remove the
  entry's vec rows (Task 2.2 acceptance). — Cost if wrong: a reviewed finding stays invisible to the
  vector leg, or a deleted entry keeps matching.
- **C3. `since` must work timezone-aware on both legs.** `published_day()` raises on an aware
  datetime while `POST /kb/search` accepts one; normalise inside `published_day` and test both legs
  with both spellings (Task 2.2).
- **C4. UI still owed from Phase 1:** Purge, new-topic and merge buttons (the API exists) → Task 2.7.
  Snapshot retention → Phase 4.
- **C5. `services.settings` ↔ `app.kb` import direction** works and is documented in
  `backend/CLAUDE.md`; no task may add an import from `app.services.settings` into `app.kb.models`
  or the reverse at module import time.

### Phase 2 (`feat/kb-vectors`)

- **P2-1. Task 2.1 owns every new settings key of the phase** — `voyage_api_key` (masked),
  `kb_embedding_model`, `kb_capture_findings`, `kb_compile_mode|model|effort|prompt|max_chars`,
  `kb_compile_monthly_token_budget`, `kb_auto_accept_suggestions`, `kb_reviewed_only`,
  `kb_recency_boost`, `kb_rerank`, `kb_duplicate_threshold` — including their API exposure. Other
  tasks only read them. `kb_compile_max_chars` (24 000) is in the spec but was in no task's list.
  — Why: one writer for `services/settings.py` lets tasks run in parallel. Cost: a key exists one
  batch before its consumer.
- **P2-2. New routes get their own router modules**: `app/api/kb_bulk.py` (2.3) and
  `app/api/kb_compile.py` (2.5), mounted under `/api/kb`. Task 2.3 alone edits
  `app/api/__init__.py` and creates a stub `kb_compile.py`; Task 2.5 fills it. `schemas/kb.py`
  belongs to 2.5; 2.3 uses `schemas/kb_bulk.py`.
- **P2-3. Branch name is `feat/kb-vectors`**, PR base `main`.
- **P2-4. The Inbox already has multi-select (`BulkBar`)**, so the bulk "Save to knowledge base"
  action, with progress and Cancel, ships in **Task 2.7**, and Task 2.3's browser acceptance is
  reachable this phase. Task 3.5 keeps the badge, `kb_entry_id`, the single-row action and `kb` in
  global search.
- **P2-5. `embed_pending` already exists (Phase 1) with another shape**: one Voyage call for the
  whole selection, `int` return, no activity row. Task 2.1 makes it write `embedded_at` **per
  batch**, keeps the `int` return (its callers live in `capture.py`) and adds the `kb_activity`
  row with the Voyage token count. — Why: the plan's "a 429 on the second batch leaves 8 of 11
  embedded" cannot hold otherwise.
- **P2-6. Findings capture lives in a new `app/kb/findings.py`** (Task 2.6), delegating to
  `capture_article(kind="finding", authorship="model", …)` the way `capture_note` does;
  `capture.py` belongs to Task 2.3 this phase.
- **P2-8. `compile_if_auto` ships in Task 2.5 with a unit test; wiring it into the three capture
  call sites** (`kb/service.py`, `api/items.py`, `api/notes.py`) is a separate integration commit
  after batch B. — Cost if forgotten: `kb_compile_mode: auto` is a setting with no effect; the
  final review checks it.
- **P2-9. `searchable()` stays synchronous** (`builtin.py`'s `__post_init__` is sync). Task 2.1
  adds an async builder and switches `get_kb_service` **and** `agent/providers.py`; missing the
  second leaves the two chat tools keyword-only with no error.
- **P2-10. `DEFAULT_COMPILE_PROMPT` and `COMPILE_PROMPT_VERSION` live in `services/settings.py`**
  beside `DEFAULT_NOTE_TEMPLATE` (the settings defaults need them at import time);
  `app/kb/prompts.py` re-exports them.
- **P2-11. `oneshot.py` also exports `structured_call_result(...) -> StructuredResult(data, usage,
  model, stop_reason)`**; `structured_call` stays the `-> dict` convenience. — Why: the budget needs
  the usage the plan's signature hides.
- **P2-13. Shared docs and `tests/conftest.py` have one writer**: tasks report their documentation
  deltas and one docs commit applies them at the end of the phase. Exception:
  `app/agent/CLAUDE.md` belongs to Task 2.4 (its acceptance requires it). `fakes/embedder.py` → 2.1,
  `fakes/anthropic.py` → 2.5.
- **P2-14. Two UI strings are deferred**: the "narrow filter — results may be incomplete" notice
  (Task 2.2 ships the `SearchOutcome.topic_filter_truncated` signal; surfacing it is Phase 3).
  Review stays `PATCH /api/kb/entries/{id}` — there is no `/review` route.
- **P2-15. Phase 2 ships `POST /api/kb/embed-pending`** (Task 2.1): user-triggered, embeds up to
  a bounded number of pending chunks per call, returns `{embedded, pending, tokens}`; Settings →
  Knowledge shows the pending count with an **Embed now** button (Task 2.7). Full re-index and
  rebuild stay in Task 4.4. — Why: without it, entries captured before the Voyage key was entered
  would stay keyword-only until Phase 4, and Task 2.7's acceptance ("enter a key → a semantic query
  finds the entry") is unreachable. Cost: one small endpoint Task 4.4 later subsumes.
- **P2-17. `POST /api/kb/search` is user-facing and returns unreviewed model-authored entries**,
  labelled by `authorship` / `review_status`. The authorship gate (S5) governs what the *model* is
  fed — the two chat tools and the notes generator, through `search_for_model` — not what the user
  sees of their own knowledge base. The Phase 2 contract first said otherwise; it is amended in the
  same commit. — Cost if wrong: one filter flag on one route.
- **P2-18. Known gap, owned by Task 4.4: an embed that straddles an embedding-model change.** A
  `PUT /api/settings` that changes `kb_embedding_model` empties `kb_chunk_vec` in one transaction
  (C1), but an `embed_pending` run already in flight — a second tab, a bulk job — still holds an
  embedder for the *old* model and can upsert its vectors after the wipe, putting two vector spaces
  in one KNN with nothing on screen to say so. It needs two concurrent user actions in a
  single-user app, so Phase 2 accepts it; Task 4.4 (re-index / rebuild, which owns the model-change
  path) must close it — e.g. `embed_pending` re-reads the configured model before each batch's
  write and drops the batch on a mismatch. — Cost if forgotten: silently degraded similarity for
  the chunks of one run, repaired only by a full re-index.
- **P2-8 (done, 05c42ab).** `KbService` carries an optional client factory; `_auto_compile` runs inside
  the capture's own `guarded` envelope, only for a newly created entry, and is a no-op without a
  factory. **The bulk job opts out**: 200 selected items would otherwise be 200 Anthropic calls from
  one click; compiling a selection stays the explicit `POST /api/kb/compile` with its estimate.
- **P2-19. In `auto` mode the star and Save requests wait for the compile call — accepted.** Spec §4.5
  says a single capture runs inside the request that caused it, and `auto` is opt-in. Moving it to a
  background task would be a second job system for one setting. It is documented (contract, route
  docstrings) and Task 2.7 gives those two actions a pending state and the toggle a sentence that says
  so, and that the only brake on `auto` is the monthly budget. — Cost if wrong: a slow star.
- **P2-20. The embedder L2-normalises every vector it returns.** `kb_chunk_vec` was created with
  sqlite-vec's default metric, which is **L2** (the frozen DDL names none), so the near-duplicate check
  converts with `cosine = 1 − d²/2` — exact only for unit vectors. Voyage's already are; normalising is
  idempotent and makes the invariant ours rather than a vendor default, so re-opening
  `output_dimension` later cannot silently switch duplicate flagging off. The plan's own brief had
  assumed a cosine metric (`1 − d`), under which nothing would ever have been flagged.
- **P2-21. Near-duplicate calibration is unvalidated.** `kb_duplicate_threshold = 0.92` (cosine) and
  the title-trigram floor of 0.8 are reasoned, not measured: "The xz backdoor" vs "The xz backdoor,
  explained" scores 0.698 and does not flag. With no Voyage key the trigram leg runs alone and
  near-identical *headlines* over different stories can flag; it only ever flags, never merges.
  Recalibrate from real use — one setting and one constant.

---

## Self-review

- **Spec coverage:** storage §4.1 → 1.1–1.3; schema evolution §4.1.1 → 1.3 + 4.4; embeddings §4.2 → 2.1; chunking §4.3 → 1.4; retrieval §4.4 → 1.5 (keyword, `fts_query`, collapse, RRF) + 2.2 (vector legs, filters-in-KNN, recency) + 3.6 (rerank); capture §4.5 → 1.6–1.7 (single, snapshots, entities, soft delete) + 2.3 (near-duplicate, bulk) + 2.6 (findings); compile §4.6 → 2.4–2.5; integration §4.7 → 1.9 (registration) + 3.1–3.5; page §4.8 → 1.8 + 2.7 + 4.1; backups §4.9 → 5.1–5.2; error handling §5 is spread across the tests named per task; testing §6 → per task; known limits §9 → the measured numbers come back from 1.5 and 2.2.
- **Ruling coverage:** S1 → 1.2, 2.2; S2 → 1.5; S3 → 1.2; S4 → 1.2, 1.6; S5 → 1.2, 2.6, 3.2; S6 → 1.9, 3.2; I1 → 1.2, 1.6, 2.3; I2 → 2.3, 3.5; I3 → 3.4; I4 → 3.1; I5 → 2.4, 2.5, 4.2; I6 → 2.5; I7 → 1.5, 2.2, 3.6; I8 → 5.1, 5.2; I9 → 1.2, 2.1, 4.4; I10 → 1.3, 4.4; I11 → 1.2, 1.6; I12 → 1.2, 1.6, 3.4; I13 → 1.1, 1.5, 2.2, 4.3; I14 → 3.3; I15 → the phase cut itself; I16 → 2.5, 2.7, 4.1.
- **Phase 1 is acceptable on its own:** no Voyage key, no Anthropic call, no hand-inserted row — the acceptance is "save an article, find it, read it, annotate it, delete it, undo". Everything irreversible (vec0 columns, tokenizer, `published_at`, `authorship`, `deleted_at`, chunk `AUTOINCREMENT`, the tool names and descriptions) is decided in it, which is the whole point of the re-cut.
- **Placeholders:** none. The two numbers the spec leaves open (§9's measured KNN ceiling and the FTS5 timing) are produced by Tasks 1.5 and 2.2 and written back into the spec in those PRs. `moto[s3]` is decided, with a named fallback and a decision point inside Task 5.1 rather than across phases.
- **Type consistency:** `Embedder` / `Reranker` / `KnowledgeStore` / `hybrid_search` / `Hit` are identical from Phase 1 to Phase 3 — `Hit` carries the raw distance from the start so the near-duplicate check and the reranker need no contract change. `CaptureResult` / `RefreshResult` / `CompileResult` / `EmbedResult` / `BackupInfo` / `RestoreInfo` used consistently. `ToolResult.content` widens once, in Task 3.1, and never narrows.
