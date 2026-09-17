# Knowledge base: design

*2026-09-17. Approved in conversation; this is the written record. The phased
implementation plan is `docs/superpowers/plans/2026-09-17-knowledge-base.md`.*

## 1. Why

Every layer of the app is ephemeral: the inbox is triage, a chat session is one-off
research, a note is one meeting. The knowledge base (KB) is the durable layer under
all three. It compounds: the chat can answer "have we covered this vendor before?",
notes can cite prior coverage automatically, and a year of saved articles becomes
"everything we discussed about supply-chain attacks" without anyone filing anything.

It grows by use. Starting from nothing, the things the user already does — star an
item, finish a research turn, save a note — put knowledge in, and every later
question gets that knowledge back out.

## 2. Decisions taken

| Question | Decision | Why |
|---|---|---|
| Where the KB lives | **In the app's SQLite file**: vectors in a `sqlite-vec` `vec0` table, keyword index in FTS5, everything behind a `KnowledgeStore` interface | One file holds all state, so backup and restore stay consistent. A hosted vector DB would hold only the derivable part (vectors) and split the state across two systems. The interface leaves the door open if the KB ever outgrows SQLite. |
| Durability / replication | **Encrypted snapshots of the whole database to S3**, taken on demand or after KB events, restored on a fresh machine from `.env` alone | Losing the machine must not lose the KB. The database also holds feeds, chats, notes and both API keys, so replicating it replicates everything. Client-side encryption because the file contains secrets. |
| Embeddings | **Voyage AI** (`voyage-4`, 1024 dims) over HTTPS, key stored masked in Settings, `VOYAGE_API_KEY` env override | Anthropic's recommended provider; no local model of any kind (no GPU). Hybrid search keeps the KB useful with no key. |
| Summaries and topic suggestions | The existing Anthropic runner (`persist=False`, no tools, structured output) | Same refusal/fallback handling as chat; one call per entry. |
| How knowledge enters | **Auto-capture + review**: notes, starred items, cited chat findings, explicit saves | Grows by use; nothing silent — every entry is visible, editable, deletable. |
| Quality and spend | **Capture is separated from compile**; compile has a mode, a model, a budget, an editable prompt, a review gate, and an activity log | The user asked for control over how knowledge is compiled and what it costs. Capture (snapshot, chunk, embed) costs cents and carries no quality risk; compile (LLM summary + topics) is where both live. |
| Topics | Curated taxonomy owned by the user; the model only suggests | Browsing, per-topic digests, "previously covered" in notes. |
| Local AI | **None.** Every embedding and every summary is an API call. | The machine has no GPU. |

Ground rules that still hold: no schedulers, no cron, no background polling (every
KB action is an event from a user action); the Anthropic key stays write-only over
the API and the same is true of the Voyage key; model output stays untrusted.

The one ground rule that changes: the app is no longer strictly local-only. With
backups configured, an encrypted copy of the database leaves the machine, to a
bucket the user owns. Without them, nothing changes.

## 3. Vocabulary

- **Entry** — one unit of knowledge: an article, a note, a finding, or a manual save.
  Has a snapshot (full text), an optional compiled summary, the user's own notes,
  topics, tags, back-links.
- **Capture** — creating an entry from a source and indexing it: snapshot, chunk,
  FTS row, embedding. Cheap (Voyage tokens only).
- **Compile** — the LLM pass over an entry: summary and topic/tag suggestions.
  Costs Anthropic tokens; governed by the compile controls.
- **Finding** — a chat turn's question plus the answer the model gave, with the
  sources it cited. Captured only when the turn ended normally and cited at least
  one source.
- **Snapshot (KB)** — an entry's saved text. **Snapshot (backup)** — an encrypted
  copy of the whole database in S3. The plan keeps the two words apart with
  "text snapshot" and "backup".

## 4. Architecture

```
backend/app/kb/
  models.py       kb_entries, kb_chunks, kb_chunks_fts (FTS5), kb_chunk_vec (vec0),
                  topics, kb_entry_topics, kb_entry_tags, kb_entry_links, kb_activity
  chunking.py     split_markdown(text, target_tokens=800, overlap_tokens=80) -> [Chunk]
  embeddings.py   Embedder protocol; VoyageEmbedder (httpx2); NullEmbedder
  store.py        KnowledgeStore protocol; SqliteKnowledgeStore (vec0 + FTS5)
  retrieval.py    hybrid_search(): vector KNN + BM25, reciprocal rank fusion, filters
  capture.py      capture_article / capture_note / capture_finding / capture_url;
                  dedup; near-duplicate check; activity rows
  compile.py      compile_entry(): summary + topic suggestions via the runner;
                  budget accounting; prompt versioning
  service.py      the façade the API and the agent tools call
backend/app/backup/
  snapshot.py     VACUUM INTO -> gzip -> AES-GCM -> S3 put; list; prune; restore
  api.py          POST /backup/run, GET /backup/status, POST /backup/restore
backend/app/api/kb.py      REST for entries, topics, search, compile, settings
backend/app/agent/builtin.py   + search_knowledge_base, get_kb_entry
frontend/src/pages/Knowledge.tsx + components/kb/**
```

Everything the KB does is reached through `service.py`. The agent tools, the notes
generator, global search and the REST layer call it; none of them touch the store or
the embedder directly.

### 4.1 Storage

`sqlite-vec` is loaded on every connection the engine opens (an `on_connect`
hook in `app/db/engine.py`, mirroring how WAL and the pragmas are set). Verified on
2026-09-17 under uv's CPython 3.12 on macOS arm64: `vec0` KNN and FTS5 both work
(`vec_version() = v0.1.9`, SQLite 3.47.1). The first plan task re-verifies inside
the Docker image.

Tables (all additive; `ADDED_COLUMNS` is not needed — these are new tables created
by `create_all`, and the two virtual tables are created by `init_db` with explicit
DDL because SQLAlchemy does not model them):

```
kb_entries       id · kind ('article'|'note'|'finding'|'manual') · title ·
                 url (canonical, NULL for notes) · source_name ·
                 feed_item_id FK NULL · note_id FK NULL · session_id FK NULL ·
                 turn_message_id FK NULL ·
                 snapshot_md TEXT · snapshot_chars INT ·
                 summary_md TEXT NULL · compiled_at DATETIME NULL ·
                 compile_model TEXT NULL · compile_prompt_version INT NULL ·
                 compile_input_tokens INT · compile_output_tokens INT ·
                 notes_md TEXT DEFAULT '' ·
                 review_status ('unreviewed'|'reviewed') · captured_by ('auto'|'user') ·
                 embedding_status ('pending'|'done'|'skipped') · embedding_model TEXT NULL ·
                 captured_at · updated_at
                 UNIQUE(url) WHERE url IS NOT NULL   (partial index)
                 UNIQUE(note_id) WHERE note_id IS NOT NULL
                 UNIQUE(turn_message_id) WHERE turn_message_id IS NOT NULL
kb_chunks        id · entry_id FK CASCADE · ord · text · token_estimate ·
                 kind ('body'|'summary')
kb_chunks_fts    FTS5(text, content='kb_chunks', content_rowid='id')  + the three
                 content-sync triggers
kb_chunk_vec     vec0(chunk_id INTEGER PRIMARY KEY, embedding float[1024])
topics           id · name UNIQUE · description · color · created_at
kb_entry_topics  entry_id FK · topic_id FK · suggested BOOL · PK(entry_id, topic_id)
kb_entry_tags    entry_id FK · tag · suggested BOOL · PK(entry_id, tag)
kb_entry_links   entry_id FK · session_id FK NULL · note_id FK NULL ·
                 feed_item_id FK NULL · created_at
kb_activity      id · at · action ('capture'|'compile'|'recompile'|'skip'|'merge'|
                 'reindex'|'backup'|'restore'|'budget_hit') · entry_id NULL ·
                 source TEXT · model TEXT NULL · input_tokens · output_tokens ·
                 detail TEXT
```

Vectors are 1024-dim float32 (4 KB each). Ten thousand chunks are 40 MB; fine.

### 4.2 Embeddings

`Embedder` protocol: `embed_documents(texts) -> list[list[float]]`,
`embed_query(text) -> list[float]`, `model: str`, `dimensions: int`.

- `VoyageEmbedder` posts to `https://api.voyageai.com/v1/embeddings` with
  `input_type` `document` or `query`, batches of ≤128 texts, `httpx2`, timeouts, and
  maps 401/429/5xx to a typed `EmbeddingError`. The key comes from
  `settings.voyage_api_key` (masked read-back like the Anthropic key) with the
  `VOYAGE_API_KEY` environment override. Model name is a setting (default `voyage-4`).
- `NullEmbedder` is used when no key is configured: capture still runs, chunks get
  `embedding_status='pending'`, and the entry is keyword-searchable. Re-index embeds
  pending chunks later.
- Tests use `FakeEmbedder` (deterministic vectors from a hash of the text) — no
  network, ever.
- Changing the embedding model marks every chunk pending; re-index rebuilds. The
  vec table is dropped and recreated if the dimension changes.

### 4.3 Chunking

Markdown-aware, paragraph-first: split on headings and blank lines, pack paragraphs
to about 800 tokens (estimated as chars/4), overlap the last 80 tokens into the next
chunk, never split inside a fenced code block. The compiled summary is stored as its
own `summary` chunk so entry-level matches are cheap. Pure function, unit-tested.

### 4.4 Retrieval

`hybrid_search(q, *, topic_ids=None, kinds=None, since=None, reviewed_only=False,
limit=20) -> [Hit]` where `Hit = {entry, chunk, snippet, score, matched_by}`:

1. Vector leg: embed the query (skipped when the embedder is null), KNN top-50 over
   `kb_chunk_vec` joined to `kb_chunks`/`kb_entries` with the filters applied.
2. Keyword leg: FTS5 `bm25()` top-50 with the same filters; the query is passed
   through the app's one escaping rule so user text cannot break FTS syntax.
3. Reciprocal rank fusion (k=60) over chunk ids; collapse to the best chunk per
   entry; return `limit` entries with the matched chunk as the snippet and
   `matched_by` in `{vector, keyword, both}` so the UI can say why.

The chat tool, the KB page, notes generation and global search all call this.

### 4.5 Capture

Every capture is the direct consequence of a user action; there is no crawler.

| Trigger | Entry kind | Text snapshot | Dedup key |
|---|---|---|---|
| Star an inbox item (policy: on) | article | extracted `content_text`; runs extraction if missing; falls back to the RSS summary | canonical URL |
| "Save to knowledge base" on an inbox item, a chat source, or a pasted URL | article / manual | fetched through `fetch_article` (URL guard, caps) | canonical URL |
| Note saved or generated (policy: on) | note | the note body | note id |
| A chat turn ends normally with an answer that cites ≥1 source (policy: on) | finding | the question + the answer text + a sources list | turn message id |

Capture steps: canonicalise the URL (strip tracking params, fragments); dedup on
the key (existing entry → add a back-link, refresh nothing); check the minimum
length (`kb_min_snapshot_chars`, default 400) and skip below it with an activity
row; write the entry and its chunks (FTS rows follow by trigger); embed body chunks
(or mark pending); near-duplicate check — if the best vector match to the new
summary chunk scores above `kb_duplicate_threshold` (cosine ≥ 0.92, default) the
entry is created but flagged `possible_duplicate_of` in the activity detail and the
review strip offers **Merge** (keeps one entry, unions links/topics/tags); then
compile if the compile mode says so. Auto-captured entries get
`captured_by='auto'`, `review_status='unreviewed'`.

Capture runs inside the request or turn that triggered it, after the triggering
write has committed, and never holds a database transaction across the embedding
call. A capture failure (Voyage down, extraction failed) is logged to `kb_activity`
and does not fail the user's original action.

### 4.6 Compile and its controls

`compile_entry(entry_id)` sends the snapshot (truncated to `kb_compile_max_chars`,
default 24 000) plus the current topic list to the runner with `persist=False`, no
tools, `output_config.format` for a JSON schema:

```
{ summary_md: string,          // 3–8 bullet points, what/why it matters/actions
  topic_ids: int[],            // existing topics only
  new_topic: {name, description} | null,   // at most one
  tags: string[] }             // ≤ 8
```

Stored: `summary_md`, `compiled_at`, `compile_model`, `compile_prompt_version`,
token counts; suggested topics/tags as `suggested=true` until the user confirms.
A refusal or fallback is handled exactly as chat handles it; the entry stays
uncompiled with the reason in `kb_activity`.

**Controls** (all in Settings → Knowledge, all stored in the `settings` kv table):

| Setting | Default | Meaning |
|---|---|---|
| `kb_capture_notes` / `kb_capture_starred` / `kb_capture_findings` | on / on / on | per-source capture policy; manual saves are always allowed |
| `kb_compile_mode` | `manual` | `manual` (entries wait; a "Compile N entries" button shows a token estimate first) or `auto` (compile at capture) |
| `kb_compile_model` | `claude-sonnet-5` | the model for summaries; any model from `/api/models` |
| `kb_compile_effort` | `low` | effort for compile calls |
| `kb_monthly_token_budget` | 2 000 000 | input+output tokens for compile per calendar month; at the limit compile stops (capture continues) and an activity row `budget_hit` is written; the counter is derived from `kb_activity` |
| `kb_compile_prompt` | shipped default | the compile prompt template; bumping it bumps `compile_prompt_version` |
| `kb_reviewed_only` | off | when on, only `reviewed` entries are returned to the chat tool and notes generation (the KB page always shows everything) |
| `kb_min_snapshot_chars`, `kb_duplicate_threshold` | 400, 0.92 | noise controls |

Per-entry actions: Compile / Recompile, Mark reviewed, Edit summary, Merge into…,
Delete. The token estimate for "Compile N" uses `messages.count_tokens` on the
prompt the batch would send.

### 4.7 Integration points

- **Chat tools** (`app/agent/builtin.py`): `search_knowledge_base(q, topic?, since?,
  limit?)` returns the hits as **`search_result` content blocks** (`source` =
  `kb://entry/{id}`, `title`, `content` = the matched chunk plus the summary,
  `citations.enabled = true`) so the model cites KB passages exactly as it cites web
  results; `get_kb_entry(id)` returns the full entry. The system prompt gains one
  paragraph: check the knowledge base before the web for "have we seen / covered /
  discussed" questions, and say when the KB had nothing. The frontend maps
  `kb://` citations to links into the Knowledge page.
- **Notes generation**: before drafting, `hybrid_search` runs per starred item
  (title + CVE ids); hits older than the current items become a "Previously
  covered" section with links. The note template gains an optional
  `{{previously_covered}}` slot; absent from the template → section skipped.
- **Inbox**: a "Saved" badge on items with a KB entry; "Save to knowledge base" as a
  row action and bulk action.
- **Global search**: a `kb` entity type routed to `hybrid_search`.
- **Rail**: a "Knowledge" page between Notes and the bottom group.

### 4.8 Knowledge page

- Left: topic list with counts (and "Unreviewed", "Uncompiled" smart filters).
- Top: search box (hybrid; each hit shows the snippet and a small `vector` /
  `keyword` marker), kind and date filters.
- Main: timeline of entries, newest first, grouped by day like the chat list.
- Review strip at the top when unreviewed entries exist: "New in the knowledge
  base (7)" → confirm suggested topics with one click, Merge for flagged
  duplicates, Skip (deletes), Compile.
- Entry detail: summary (editable), text snapshot (collapsed), notes editor
  (Markdown, autosave), topics/tags editor, back-links (sessions, notes, items),
  related entries (same topics, recent), activity for this entry.
- Settings → Knowledge: Voyage key (masked), embedding model, compile controls,
  budget with month-to-date counter, index stats (entries, chunks, pending), Re-index,
  activity log (last 200 rows).

### 4.9 Backups and restore

`backend/app/backup/snapshot.py`:

- **Take**: `VACUUM INTO <tmp>` on the live engine (consistent under WAL), gzip,
  encrypt with AES-256-GCM using a key derived from `BACKUP_PASSPHRASE` (scrypt,
  random salt and nonce stored in the file header), upload to
  `s3://{bucket}/{prefix}/{utc timestamp}.db.gz.enc` and update
  `{prefix}/latest.json` (timestamp, size, sha256, app version). Uses `boto3`
  with the standard AWS credential chain (`AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` / `AWS_REGION`, or a profile). Keeps the last
  `BACKUP_KEEP` (default 30) objects; pruning happens in the same run.
- **Triggers**: "Back up now" in Settings; optional `kb_backup_after_capture`
  (event-driven, coalesced: at most one upload per 5 minutes, the last event wins).
  No timer ever fires on its own.
- **Restore**: on startup, if the database file does not exist and
  `BACKUP_RESTORE_ON_EMPTY=1` and credentials are present, download `latest`,
  decrypt, verify the sha256, place it as the database, then continue booting;
  also "Restore from cloud…" in Settings with a confirm dialog (replaces the
  current database after taking a local `.pre-restore` copy). The restored file
  carries both API keys, so a new machine needs only `.env`.
- **What is in the snapshot**: everything, including the KB vectors — no
  re-embedding after a restore.
- Status endpoint: last backup time, size, count, last error; shown in Settings.

`.env.example` gains `BACKUP_S3_BUCKET`, `BACKUP_S3_PREFIX`, `BACKUP_PASSPHRASE`,
`BACKUP_KEEP`, `BACKUP_RESTORE_ON_EMPTY`, and the AWS variables. The passphrase is
never stored in the database.

## 5. Error handling

- Voyage unavailable or no key: capture completes with pending embeddings;
  keyword search still works; the KB page shows "N chunks not embedded" with
  Re-index.
- Compile refusal / API error / budget hit: entry stays uncompiled, reason in
  `kb_activity`, visible in the review strip.
- Extraction failure on save: entry created from the RSS summary if long enough,
  else skipped with an activity row.
- Backup failure: shown in Settings status, never blocks the user's action.
- Restore with a wrong passphrase: decryption fails closed; the existing database is
  untouched.
- Deleting a note, session or item: the KB entry survives (it is a snapshot);
  the back-link row is removed by FK cascade.

## 6. Testing

- Pure: chunking (boundaries, code fences, overlap), fusion (ties, one leg empty),
  URL canonicalisation, token estimate, budget arithmetic, near-duplicate
  decision, `search_result` block shaping.
- Store: real `sqlite-vec` + FTS5 in the test database (extension loaded by the
  engine hook), `FakeEmbedder`; KNN order, filters, dimension change → rebuild.
- Capture: each trigger through the API with the scripted Anthropic fake and the
  fake embedder; dedup; policy off → no entry; failure isolation.
- Compile: structured output parsed; suggestions stored as suggested; budget stop;
  refusal path; prompt version bump.
- Tools: `search_knowledge_base` returns `search_result` blocks; `reviewed_only`
  respected; the runner accepts them (scripted fake).
- Backup: round-trip take → encrypt → decrypt → restore against a temp
  directory with a fake S3 (`moto` or an in-memory `boto3` stub — the plan picks
  one); prune; wrong passphrase fails closed.
- Frontend (vitest): grouping, hit rendering helpers, citation → KB link mapping,
  budget formatting.
- Browser passes per PR on a trial stack.

## 7. Out of scope (for now)

Hosted vector database; local models; automatic crawling of feeds into the KB;
multi-user; sharing; scheduled backups (would need a timer — if wanted later, it is
a host cron calling `POST /backup/run`).
