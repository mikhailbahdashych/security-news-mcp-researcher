# Roadmap — features after the core build

Status: **backlog**. Nothing here is scheduled until the seven core PRs (scaffold → db/settings → feeds/inbox → research chat → MCP client → notes → history/search) are merged and stable. Everything keeps the product's ground rules: local-only, single user, no auth, no deployment, no schedulers — every action is user-triggered.

Priority order (recommended): 1 → 2 → 3 → 4, then the rest as appetite allows.

---

## 1. Knowledge base with topics (highest value)

> **Designed 2026-09-17, revised after an adversarial critique the same day, and now
> partly built.** The approved design is
> `docs/superpowers/specs/2026-09-17-knowledge-base-design.md` (v2); the phased plan and
> its Decisions log are `docs/superpowers/plans/2026-09-17-knowledge-base.md`. Those two
> are the only description of this feature worth reading — the sketch that used to sit
> here described a schema (`snapshot_md`, `kb_entry_links`, "search stays `LIKE`-based")
> that the build superseded; `backend/app/kb/models.py` is what actually shipped.
> **Status: Phase 1 (the keyword knowledge base — PR #30) and Phase 2 (Voyage
> embeddings, hybrid retrieval, near-duplicates, bulk capture, compile with a monthly
> token budget, model-authored findings — PR #32) are shipped. Phases 3–5 — deeper
> chat/notes integration and reranking, curation/digests/export/re-index, encrypted
> backups — are in the plan and are not scheduled.**

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
- More default sources: vendor advisories (MSRC, GitHub Security Advisories, AWS Security Bulletins), NVD recent CVEs.
- Expression index on `COALESCE(published_at, fetched_at)` once the inbox is large.
