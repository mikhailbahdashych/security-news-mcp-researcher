# Knowledge Base Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A durable, searchable knowledge base that grows from normal use of the app, feeds the chat and the notes, and survives the loss of the machine.

**Architecture:** An embedded RAG engine inside the app's SQLite file (`sqlite-vec` vectors + FTS5 keywords, fused), Voyage embeddings over HTTPS, LLM compile through the existing runner, encrypted whole-database backups to S3. Everything behind `app/kb/service.py`.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2 async + aiosqlite / `sqlite-vec` / FTS5 / `httpx2` (Voyage) / `boto3` + `cryptography` (backups) / pytest with fakes — React 19 / TS / TanStack Query / vitest.

**Spec:** `docs/superpowers/specs/2026-09-17-knowledge-base-design.md`

**How this plan is used:** it is the phase-and-task map. Each phase is one PR. When a phase starts, its tasks are expanded into step-level briefs (failing test → run → implement → run → commit, with the exact code) from this document and the spec, so code snippets never go stale across five phases. Every task below states its files, its interfaces, its tests and its acceptance check.

## Global Constraints

- No schedulers, no cron, no background polling. A capture, compile or backup runs only as the consequence of a user action, inside the request or turn that caused it.
- No local models. Embeddings = Voyage API; summaries = Anthropic API.
- No network in tests: `FakeEmbedder`, the scripted Anthropic fake, `httpx2.MockTransport`, a fake S3.
- `httpx2`, never `httpx`. Naive-UTC datetimes via `utcnow()`. New columns on existing tables go through `ADDED_COLUMNS`; new tables through `create_all`; the two virtual tables through explicit DDL in `init_db`.
- The Voyage key is write-only over the API (masked read-back), same as the Anthropic key. `BACKUP_PASSPHRASE` lives only in the environment.
- Every LLM call goes through `app/agent/runner.run(persist=False, ...)` with the app's refusal/fallback handling; no direct `messages.create`.
- Plain commit messages, explicit `git add`, no attribution trailers, never merge, never push to main.
- Frontend: tokens only, the `embedded` contract, no jsdom (pure logic in `.ts`).

---

## Phase 1 — Foundation (PR `feat/kb-foundation`)

Outcome: the engine exists and is searchable through the API and Settings, with nothing captured automatically yet.

### Task 1.1: Prove the extension loads everywhere
- **Files:** `backend/pyproject.toml` (+`sqlite-vec>=0.1.9,<0.2`), `backend/app/db/engine.py` (`on_connect` hook that loads `sqlite_vec` and asserts `vec_version()`), `backend/tests/test_db.py`.
- **Interfaces:** every engine connection has `vec0` and FTS5 available.
- **Tests:** a connection can create a `vec0` table and run a KNN query; FTS5 compile option is present.
- **Acceptance:** pytest green on macOS; `docker build` + `python -c "import sqlite3, sqlite_vec; ..."` inside the image prints the vec version (recorded in the PR body). If the base image's `sqlite3` lacks `enable_load_extension`, the task switches the runtime to a Python build that has it — decided and documented here, not later.

### Task 1.2: Schema
- **Files:** `backend/app/kb/__init__.py`, `backend/app/kb/models.py`, `backend/app/db/init.py` (virtual tables + FTS content-sync triggers), `backend/app/db/models.py` (import so `create_all` sees the tables).
- **Interfaces:** ORM classes `KbEntry`, `KbChunk`, `Topic`, `KbEntryTopic`, `KbEntryTag`, `KbEntryLink`, `KbActivity` exactly as the spec §4.1; `EMBEDDING_DIMENSIONS = 1024`.
- **Tests:** tables exist after `init_db`; the partial unique indexes reject a second entry with the same URL / note id / turn id and accept NULLs; inserting a chunk populates the FTS row; deleting an entry cascades chunks, FTS rows and vec rows.
- **Acceptance:** `init_db` twice is idempotent.

### Task 1.3: Chunking
- **Files:** `backend/app/kb/chunking.py`, `backend/tests/test_kb_chunking.py`.
- **Interfaces:** `split_markdown(text: str, *, target_tokens=800, overlap_tokens=80) -> list[Chunk]` with `Chunk(ord, text, token_estimate)`; `estimate_tokens(text) -> int` (chars/4, rounded up).
- **Tests:** empty → []; short text → one chunk; paragraphs packed to the target; a fenced code block is never split; overlap carries the tail of the previous chunk; headings start a new chunk.
- **Acceptance:** pure, no I/O.

### Task 1.4: Embedders
- **Files:** `backend/app/kb/embeddings.py`, `backend/tests/fakes/embedder.py`, `backend/tests/test_kb_embeddings.py`, `backend/app/services/settings.py` (+`voyage_api_key`, `kb_embedding_model` keys, masked read-back, `VOYAGE_API_KEY` env precedence mirroring `external_api_key`).
- **Interfaces:** `class Embedder(Protocol): model: str; dimensions: int; async embed_documents(texts) -> list[list[float]]; async embed_query(text) -> list[float]`; `VoyageEmbedder(api_key, model, transport=None)`; `NullEmbedder`; `FakeEmbedder` (hash → unit vector, deterministic); `EmbeddingError(status, message)`; `build_embedder(session) -> Embedder` (Null when no key).
- **Tests:** request shape (`input_type`, batching at 128, auth header) via `MockTransport`; 401/429/500 → typed errors; `FakeEmbedder` determinism; settings masking; env override wins.
- **Acceptance:** no real network in tests.

### Task 1.5: Store and retrieval
- **Files:** `backend/app/kb/store.py`, `backend/app/kb/retrieval.py`, `backend/tests/test_kb_store.py`, `backend/tests/test_kb_retrieval.py`.
- **Interfaces:** `KnowledgeStore` protocol: `upsert_vectors(chunk_ids, vectors)`, `delete_vectors(chunk_ids)`, `knn(query_vec, k, *, entry_filter) -> [(chunk_id, distance)]`, `keyword(q, k, *, entry_filter) -> [(chunk_id, bm25)]`, `rebuild(dimensions)`; `SqliteKnowledgeStore(session_factory)`; `hybrid_search(session_factory, embedder, q, *, topic_ids, kinds, since, reviewed_only, limit) -> list[Hit]`; `rrf(rankings, k=60)` pure.
- **Tests:** KNN order with `FakeEmbedder`; keyword escaping (a query containing `"` or `*` does not raise); filters applied on both legs; RRF ties and one-leg-empty; `NullEmbedder` → keyword-only with `matched_by='keyword'`; dimension change → rebuild.
- **Acceptance:** search over 200 synthetic chunks returns in well under 100 ms in the test.

### Task 1.6: Search API, KB settings, docs
- **Files:** `backend/app/api/kb.py` (`POST /api/kb/search`, `GET /api/kb/stats`), `backend/app/api/settings.py` (+Voyage key set/clear, embedding model), `backend/app/schemas/kb.py`, `backend/app/main.py` (router), `frontend/src/api/kb.ts`, `frontend/src/components/settings/KnowledgeSection.tsx` (key, model, stats), `backend/CLAUDE.md`, `frontend/CLAUDE.md`, `docs/DESIGN.md` (KB section), `.env.example` (`VOYAGE_API_KEY`).
- **Tests:** API contract tests; the raw Voyage key never appears in any response body; stats counts.
- **Acceptance:** with a real key entered in Settings on a trial stack, `POST /api/kb/search` against a hand-inserted entry returns a vector hit (recorded in the PR body).

---

## Phase 2 — Capture, compile, and the Knowledge page (PR `feat/kb-capture`)

Outcome: the KB grows by use, with the controls the user asked for.

### Task 2.1: Capture service
- **Files:** `backend/app/kb/capture.py`, `backend/app/kb/urls.py` (`canonical_url`), `backend/tests/test_kb_capture.py`.
- **Interfaces:** `capture_article(sf, embedder, *, url, title, source_name, text, feed_item_id, captured_by) -> CaptureResult`; `capture_note(sf, embedder, note_id)`; `capture_finding(sf, embedder, *, session_id, turn_message_id, question, answer, sources)`; `capture_url(sf, embedder, url)` (through `fetch_article` + `extract_article`); `CaptureResult(entry_id, created: bool, skipped_reason, possible_duplicate_of)`; `near_duplicate(store, vector, threshold) -> entry_id | None`.
- **Tests:** each kind creates an entry + chunks + FTS + vectors; dedup adds a back-link, not an entry; minimum length skip with an activity row; near-duplicate flag; embedder failure → entry with pending chunks, action still succeeds; never holds a transaction across `embed_documents`.

### Task 2.2: Capture triggers with policy
- **Files:** `backend/app/api/items.py` (star → capture when `kb_capture_starred`), `backend/app/api/notes.py` / `services/notes.py` (save/generate → capture when `kb_capture_notes`), `backend/app/agent/turns.py` (turn end → `capture_finding` when `kb_capture_findings` and the answer cited ≥1 source and the turn ended normally), `backend/app/api/kb.py` (`POST /api/kb/entries` manual save from item id or URL), settings keys `kb_capture_*`, `kb_min_snapshot_chars`, `kb_duplicate_threshold`.
- **Tests:** each trigger through the API with fakes; policy off → no entry; a stopped turn → no finding; an answer without sources → no finding; a capture error never fails the original request.

### Task 2.3: Compile with controls
- **Files:** `backend/app/kb/compile.py`, `backend/app/kb/prompts.py` (default prompt, `COMPILE_PROMPT_VERSION`), settings keys `kb_compile_mode|model|effort|prompt`, `kb_monthly_token_budget`, `backend/app/api/kb.py` (`POST /api/kb/entries/{id}/compile`, `POST /api/kb/compile` batch with `?estimate=1` returning a `count_tokens` estimate, `GET /api/kb/budget`), `backend/tests/test_kb_compile.py`.
- **Interfaces:** `compile_entry(sf, client_factory, entry_id) -> CompileResult`; `month_usage(sf) -> {input, output}` derived from `kb_activity`; `budget_allows(sf, estimate) -> bool`.
- **Tests:** structured output parsed and stored; suggestions stored as `suggested`; unknown topic ids dropped; `new_topic` creates nothing until confirmed; budget hit → `budget_hit` activity and no call; refusal path via the scripted fake; prompt version recorded; `auto` mode compiles at capture, `manual` does not.

### Task 2.4: Entries, topics and review API
- **Files:** `backend/app/api/kb.py` (entries list/detail/patch/delete, topics CRUD, `POST /entries/{id}/review`, `POST /entries/{id}/merge`, `GET /activity`), `backend/app/schemas/kb.py`.
- **Tests:** list filters (topic, kind, since, review, compiled); patch notes/summary; confirm suggestions; merge unions links/topics/tags and deletes the duplicate; delete cascades.

### Task 2.5: Knowledge page and Settings controls
- **Files:** `frontend/src/pages/Knowledge.tsx`, `frontend/src/components/kb/{TopicSidebar,EntryList,EntryDetail,ReviewStrip,SearchBox,CompileDialog}.tsx`, `frontend/src/components/settings/KnowledgeSection.tsx` (capture policy, compile mode/model/effort/prompt, budget + month-to-date, Re-index, activity log), `frontend/src/components/ui/{Rail,layout}.ts*` (new page key `knowledge`), `frontend/src/api/kb.ts`, vitest for grouping/formatting helpers, `frontend/CLAUDE.md`.
- **Acceptance (browser):** star an item → appears in the review strip; Compile N shows an estimate then compiles; confirm topics; search shows vector/keyword markers; Settings budget counter moves.

---

## Phase 3 — Integration (PR `feat/kb-integration`)

Outcome: the chat and the notes use the KB; the inbox and global search know about it.

### Task 3.1: Chat tools with citations
- **Files:** `backend/app/agent/builtin.py` (+`search_knowledge_base`, `get_kb_entry`), `backend/app/agent/prompts.py` (KB paragraph), `backend/app/agent/registry.py` if tool results need a block-list shape, `backend/tests/test_agent_kb_tools.py`, `backend/app/agent/CLAUDE.md`.
- **Interfaces:** tool results returned as a list of `search_result` blocks (`source="kb://entry/{id}"`, `title`, `content=[{type:"text", text}]`, `citations:{enabled:true}`); `reviewed_only` honoured; stable tool ordering preserved (cache prefix).
- **Tests:** block shape; the scripted fake accepts it; `kb://` citations survive persistence (verbatim content); both tools are always registered, and their descriptions say the KB may be empty.

### Task 3.2: Frontend citations and sources
- **Files:** `frontend/src/api/chat.ts` (`kb://` → Knowledge link, source card), `frontend/src/components/chat/SourcesGrid.tsx`, vitest.
- **Acceptance (browser):** ask "have we covered Okta before?" with an Okta entry present → the answer cites it and the source card opens the entry.

### Task 3.3: Notes "Previously covered"
- **Files:** `backend/app/services/notes.py` (search per item, `{{previously_covered}}` slot), default template update, `backend/tests/test_notes_generate.py`.
- **Tests:** section present with hits, absent without; slot missing from a custom template → skipped.

### Task 3.4: Inbox badge, bulk save, global search
- **Files:** `backend/app/api/items.py` (`kb_entry_id` on items), `backend/app/services/search.py` (+`kb` entity), `frontend/src/components/inbox/**` (badge, row + bulk "Save to knowledge base"), `frontend/src/components/ui/GlobalSearch.tsx`.
- **Tests:** items carry the id; global search returns kb hits; bulk save dedups.

---

## Phase 4 — Curation and export (PR `feat/kb-curation`)

### Task 4.1: Topics management UI (rename, describe, colour, merge topics, counts).
### Task 4.2: Digests — `POST /api/kb/digests` (topic + since) reusing the notes generator with a digest template setting; a "Digest" button on a topic page; result saved as a note linked to the topic.
### Task 4.3: Obsidian export — `GET /api/kb/export.zip` (one `.md` per entry with YAML frontmatter) and per-topic export.
### Task 4.4: Re-index and model change — `POST /api/kb/reindex` embeds pending chunks, or everything after a model change (vec table rebuilt on a dimension change); the request runs the batch and streams progress as SSE like note generation, cancellable; no polling.

---

## Phase 5 — Cloud replication (PR `feat/kb-backup`)

### Task 5.1: Snapshot pipeline
- **Files:** `backend/app/backup/snapshot.py`, `backend/app/backup/crypto.py` (AES-256-GCM, scrypt key derivation, versioned header), `backend/pyproject.toml` (+`boto3`, `cryptography`), `backend/tests/test_backup.py` with a fake S3 (`moto[s3]` if it installs cleanly under the lock, else a minimal in-memory client stub — decided in the task brief).
- **Interfaces:** `take_backup(engine, config) -> BackupInfo`; `list_backups(config)`; `prune(config, keep)`; `restore_latest(config, db_path) -> RestoreInfo`; `BackupConfig.from_env()`.
- **Tests:** round trip through fake S3 and a temp dir; wrong passphrase fails closed; prune keeps N; `latest.json` sha verified; `VACUUM INTO` while a writer holds the engine.

### Task 5.2: API, triggers, Settings
- **Files:** `backend/app/backup/api.py` (`POST /api/backup/run`, `GET /api/backup/status`, `POST /api/backup/restore`), `backend/app/main.py` (restore-on-empty at startup when `BACKUP_RESTORE_ON_EMPTY=1`; coalesced after-capture trigger, at most one upload per 5 min, last event wins — implemented as a debounced task started by the capture event, never a timer that fires on its own), `frontend/src/components/settings/BackupSection.tsx`, `.env.example`, README, `CLAUDE.md` (the "no longer strictly local-only" paragraph).
- **Acceptance (documented in the PR):** fresh clone + `.env` with the bucket, passphrase and AWS variables → first start restores the database → the KB, chats, notes and keys are all there.

---

## Self-review

- **Spec coverage:** storage §4.1 → 1.1–1.2; embeddings §4.2 → 1.4; chunking §4.3 → 1.3; retrieval §4.4 → 1.5–1.6; capture §4.5 → 2.1–2.2; compile and controls §4.6 → 2.3–2.5; integration §4.7 → 3.x; page §4.8 → 2.5 + 4.1; backups §4.9 → 5.x; error handling §5 is spread across the tests named per task; testing §6 → per task.
- **Placeholders:** none; the one intentionally deferred choice (`moto` vs a stub) is named with its decision point.
- **Type consistency:** `Embedder`/`KnowledgeStore`/`hybrid_search`/`Hit` names are identical in Phases 1–3; `CaptureResult`/`CompileResult` used consistently.
