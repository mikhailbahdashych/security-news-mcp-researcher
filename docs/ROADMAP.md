# Roadmap — features after the core build

Status: **backlog**. Nothing here is scheduled until the seven core PRs (scaffold → db/settings → feeds/inbox → research chat → MCP client → notes → history/search) are merged and stable. Everything keeps the product's ground rules: local-only, single user, no auth, no deployment, no schedulers — every action is user-triggered.

Priority order (recommended): 1 → 2 → 3 → 4, then the rest as appetite allows.

---

## 1. Knowledge base with topics (highest value)

> **Designed 2026-09-17, revised after an adversarial critique on 2026-09-17.** The approved design is `docs/superpowers/specs/2026-09-17-knowledge-base-design.md` (v2) and the phased plan is `docs/superpowers/plans/2026-09-17-knowledge-base.md`; they supersede the sketch below where they differ (embedded RAG engine with `sqlite-vec` + FTS5, Voyage embeddings, capture/compile split with spend controls, encrypted S3 backups — and, from v2, versioned text snapshots, an authorship gate on model-written entries, filters inside the vector KNN, and a Phase 1 that is a usable keyword KB).

### Why
Today every layer of the app is ephemeral: the inbox is triage, chat sessions are one-off research, notes are per-meeting. A knowledge base is the durable layer underneath all three. It compounds over time: the chat can answer "have we covered this vendor before?", notes can cite prior coverage automatically, and a year of saved articles becomes "everything we discussed about supply-chain attacks" for free.

### What
- **Save an article** from anywhere: an inbox item, a URL the chat fetched (`fetch_article` / web results), or a pasted link. A saved entry keeps a **full-text snapshot** (security articles get edited; vendor advisories disappear), the source URL and feed, an **LLM summary**, and the user's **own notes** (Markdown).
- **Topics** = a small curated taxonomy the user owns (e.g. Supply chain, Cloud/IAM, Ransomware, AppSec, Identity, Phishing, plus vendor topics like "Okta", "GitHub"). Each topic has a name, one-line description, optional color. **Tags** = free-form, lightweight, many.
- **Auto-labelling on save**: the LLM proposes topics from the existing taxonomy (plus at most one new topic suggestion) and tags; the user confirms in one click. Suggestions only — nothing is applied silently.
- **Browse**: KB page with topic sidebar (counts), search (reuses the global search), timeline (saved_at), entry detail with snapshot / summary / notes editor / labels / "related entries" (same topics, recent) / back-links to the chat sessions and notes that referenced it.
- **Digests**: "Everything saved on <topic> since <date>" → a generated Markdown digest (reuses the notes generator with a digest template).
- **Export**: whole KB or one topic as an Obsidian-compatible folder — one `.md` per entry with YAML frontmatter (title, url, saved_at, topics, tags) and the summary + notes + snapshot. No lock-in.

### Integration points (this is where the value multiplies)
- Chat tool `search_knowledge_base(q, topic?, since?)` and `get_kb_entry(id)`; system prompt tells the model to check the KB before the web for "have we seen this before" questions.
- Notes generation: optional "Previously covered" section listing KB entries with shared topics/CVEs, so the weekly notes carry institutional memory.
- Inbox: a "Saved" badge on items already in the KB; "Save to KB" as a row action and a bulk action.
- Global search (PR 7) gains a `kb` entity type.

### Data model (additive — new tables only)
```
kb_entries      id · feed_item_id FK NULL · url · title · source_name · saved_at ·
                snapshot_md TEXT · summary_md TEXT NULL · notes_md TEXT default '' ·
                summary_model TEXT NULL · updated_at
topics          id · name UNIQUE · description · color · created_at
kb_entry_topics entry_id FK · topic_id FK · PK(entry_id, topic_id)
kb_entry_tags   entry_id FK · tag TEXT · PK(entry_id, tag)
kb_entry_links  entry_id FK · session_id FK NULL · note_id FK NULL   (back-links)
```
Search stays `LIKE`-based (single user); an FTS5 table over snapshot/summary/notes is the one place FTS would actually pay off — revisit if LIKE gets slow.

### LLM usage
- Summary + label suggestion in one call using the existing runner with `persist=False`, restricted to no tools, and a structured output schema `{summary_md, topics: [existing ids], new_topic?: {name, description}, tags: []}`. Same refusal/fallback handling as chat.
- Digest generation reuses the notes generator with a digest template stored in settings.

### Phased delivery (each a PR)
1. Tables + KB CRUD API + "Save to KB" from the inbox (snapshot via the existing extraction) + KB page (browse, topic sidebar, entry detail with notes editor). No LLM yet.
2. Topics/tags management + LLM summary & label suggestions on save.
3. Chat tools + notes "Previously covered" + inbox badges + global search integration.
4. Digests + Obsidian export.

### Open questions (decide when scheduling)
- Should saving from chat also save the model's answer excerpt about that article as the initial note? (Probably yes, marked as AI-written.)
- Topic taxonomy seed: ship a default set or start empty? (Suggest a small default set the user can delete.)

---

## 2. CVE enrichment + watchlist highlights
- Detect CVE IDs in item titles/summaries/snapshots; enrich on demand from NVD (CVSS, description) and the **CISA KEV** catalog (known exploited — the strongest "patch now" signal for a meeting). Cache enrichments locally.
- Inbox shows a compact CVE badge (score, KEV flag); notes template can pull the enrichment into "What happened".
- **Watchlist**: a local list of vendors / technologies / keywords (e.g. "Okta", "GitHub Actions", "Kubernetes") that highlights matching inbox items and can filter the inbox. Narrower than a company profile — optional, user-owned, deletable.
- Effort: small-medium. No LLM required for the enrichment itself.

## 3. Meeting-prep triage (one-click weekly pipeline)
- One button: refresh feeds → LLM scores the week's unread items by severity and relevance (criteria editable in settings, watchlist-aware if present) with a one-line rationale each → user confirms/edits the shortlist → items are starred → notes generation opens pre-filled.
- Still manual (no scheduler); it just collapses the clicks. Reuses the runner with `persist=False` + structured output.
- Effort: medium.

## 4. Follow-up / action-item tracker
- The "Recommended actions for teams" sections of generated notes become trackable items (team, action, status open/done/dropped, created from note X on date Y).
- Next week's notes open with "Outstanding from previous meetings". A tiny page to tick items off.
- This is the part of the weekly meeting that currently has no memory at all.
- Effort: small-medium (new table + parser for the template's action bullets + small UI).

## 5. Story clustering in the inbox
- The same breach is covered by five outlets; group near-duplicate items (title similarity + shared URLs/CVEs, optionally an LLM pass) into one row with sources listed. Reduces noise week to week.
- Effort: medium; heuristic first, LLM optional.

## 6. Presentation export
- Export a note as Marp Markdown slides or a clean single-page HTML for screen sharing in the meeting. Pure formatting over existing Markdown.
- Effort: small.

## 7. Expose the app as an MCP server
- Serve the inbox / KB / notes as MCP tools so Claude Desktop or Claude Code can query them ("what's in my inbox this week?", "search my KB for Log4j"). Stdio or Streamable HTTP; read-only first.
- Effort: small-medium (the services already exist; this is a thin adapter).

## 8. Trends view
- Items saved / topics / CVEs per week — a small chart for a quarterly "what did we see" review. Only worth it once the KB has months of data.
- Effort: small.

## Smaller improvements already noted during the core build
- Feeds blocked by CDN TLS fingerprinting (BleepingComputer, CISA return 403 to non-browser clients): try `curl_cffi` browser impersonation for feed fetches.
- More default sources: vendor advisories (MSRC, GitHub Security Advisories, AWS Security Bulletins), NVD recent CVEs.
- Expression index on `COALESCE(published_at, fetched_at)` once the inbox is large.
