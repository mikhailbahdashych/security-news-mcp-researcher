# Security News MCP Researcher

A local-only, single-user web app for security engineers: an RSS security-news inbox,
an LLM research chat, a meeting-notes generator and a searchable knowledge base of what
you kept. FastAPI backend, React SPA, SQLite storage — nothing leaves your machine except
the calls you ask it to make.

## Running it

Two terminals:

```sh
make dev-api     # uv run python -m app --reload — binds 127.0.0.1:$PORT (default 8000)
make dev-web     # Vite dev server on :5173, proxying /api to $PORT
```

Then open <http://localhost:5173>. The backend reads `PORT` from the environment or
the repo-root `.env`; Vite reads only the environment, so a port set in `.env` alone
needs `PORT=... make dev-web` as well.

Prerequisites: [uv](https://docs.astral.sh/uv/) and Node 22+.
Copy `.env.example` to `.env` if you want to override defaults. `CORS_ORIGINS` takes a
comma-separated list (`CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173`) as
well as a JSON array; `*` is refused. Leave it empty unless a browser on some other
origin has to call the API — `make dev-web` proxies `/api` through Vite, so the app
itself is always same-origin.

## The Anthropic API key

Set it either way — both work, neither is written to the other:

- **Settings → Anthropic API key** in the app. Stored in the SQLite database and
  only ever read back masked (`sk-ant-…a1b2`).
- **`ANTHROPIC_API_KEY`** in `.env` (copied from `.env.example`) or in the real
  process environment (`ANTHROPIC_API_KEY=... make dev-api`).

Precedence is process environment, then `.env`, then the stored key; an externally
supplied key overrides the stored one and is never saved to the database. `GET
/api/settings` reports which one is in force as `key_source`
(`env` / `stored` / `none`), while `has_api_key` means only "a key is stored in
this database".

## The inbox

Feeds are pulled on demand — press **Refresh feeds** — and each entry is deduplicated
on its feed's own guid (falling back to the entry link, then to a hash of title and
link), so refreshing twice adds nothing. Headlines are triaged star / dismiss, and
**Extract article** fetches the page behind an item and stores its text as markdown;
a paywalled or JavaScript-only page is reported rather than stored, and the feed
summary stays as the fallback. **Seed defaults** adds a starter set of security
sources (The Hacker News, BleepingComputer, Krebs on Security, CISA advisories, SANS
ISC, Google Project Zero).

## Research chat

**Research** streams an answer and shows its working: a STEPS card with the reasoning
and every tool call in the order they happened, and a SOURCES grid of what the answer
can be traced back to. The model can search and read your inbox, use Anthropic's
server-side web search and fetch (both toggleable in Settings), and call any tool from
a configured MCP server. Attach inbox items to a question from the composer, or send a
multi-select straight from the Inbox with **Research these**. A turn belongs to the
conversation, not to the page that asked for it: reload, walk off to the Inbox or close
the tab and it keeps running. Open the chat again and it is there, replayed from its
first word and still writing. **Stop** is the one thing that ends a turn early — it
cancels it server-side, so it stops billing too.

## Meeting notes

**Notes** turns starred items and/or a research session into one Markdown document,
following the template in Settings (five headings per item by default). Generation is
streamed and can be stopped; it writes **nothing** unless it finishes, so a refusal or
a stop leaves no half-note behind. A saved note records its sources — the items it was
asked about, plus any page the model deliberately fetched or actually cited. Edit it in
place, **Copy** it, or **Download** it as `.md`.

## Knowledge base

**Knowledge** is the durable layer: articles you starred, notes you saved, URLs you
pasted, each kept with its own text snapshot so a page that is later edited or taken
down is still readable. Save a whole Inbox selection at once with **Save to knowledge
base** — it streams its progress and can be stopped, and what was already saved stays.

Search works with no extra key: it is SQLite FTS5 over the snapshots. For **semantic
search**, put a Voyage AI key in **Settings → Knowledge** (or `VOYAGE_API_KEY` in `.env`,
same precedence as the Anthropic key, and it is only ever read back masked), then press
**Embed now** to work through anything captured before the key existed. From then on a
search runs both legs and each hit says which one found it.

**Compile** asks a model to summarise one entry and suggest topics, tags and vendors.
That is the only part that costs money, so it is manual by default and the dialog prices
a batch before you confirm it — a **monthly token budget** (Settings → Knowledge) stops
compiling when the month's allowance is gone, with capture and search unaffected. You can
switch it to automatic, in which case starring an item waits for the summary. The budget
counts compile tokens only; chat spend is reported per conversation instead.

Two things are deliberately quiet. A new entry that looks like one you already have is
**flagged, never merged** — you merge or dismiss. And **findings** (keeping a finished
research answer as an entry of its own) are **off** until you turn them on: they are
written by a model, so they are marked as such and never fed back to a model until you
have reviewed one.

## Search and history

`Cmd/Ctrl+K` searches your feed items, research sessions and notes at once, and a hit
opens that entity — an item deep-links into the Inbox filtered to the same query.
A session matches on anything said inside it, not just its title, so a CVE mentioned in
the middle of a long answer is findable. Sessions can be renamed, archived (hidden from
the sidebar by default) and deleted; deleting one keeps any notes generated from it.

## The window

The left rail collapses to icons or expands to labels, and the theme follows your OS
until you pick one — both remembered. **Settings → Layout** turns on **split screen**,
which puts two of the four pages side by side in one window: the left pane is the one
with the URL and the back button, the right one is a second view for reference.

## Outbound fetch safety

Everything the server fetches is influenced by someone else: a feed is third-party
content, article URLs come out of that content, and later the research agent can
propose URLs of its own. So every outbound request goes through
`backend/app/services/url_guard.py`, which resolves the host and refuses anything
that is not a public address — loopback, private ranges (10/8, 172.16/12,
192.168/16), link-local (169.254/16, including the cloud metadata endpoint),
carrier-grade NAT, IPv6 unique-local, multicast and reserved space — and re-checks
**every redirect hop**, because a redirect is the usual way past a check that only
looks at the URL you started with. Response bodies are capped, and non-http(s)
schemes are refused outright.

One exemption: the **first hop of a feed URL you typed yourself** is not checked.
Pointing this app at a FreshRSS or Miniflux instance on your own LAN is a legitimate
setup, and you are the one who configured it. Everything that URL redirects to is
still checked, and article URLs — which nobody typed — are checked from the first hop.

The app identifies itself honestly (`SecurityNewsResearcher/0.1`, not a browser).
Two of the default sources sit behind bot protection that refuses non-browser TLS
clients: when a feed or article fetch comes back `403`, the app retries it once
through a browser-TLS client (`curl_cffi`), with the same guard, redirect checks and
size caps. A `403` that survives the retry is shown on the feed as
"blocked by the site's bot protection".

## MCP servers

Settings → **MCP servers** takes the same `mcpServers` JSON you would paste into Claude
Desktop. Saved servers' tools become callable from the research chat, namespaced
`mcp__{server}__{tool}`, alongside the built-in inbox tools and Anthropic's web search.

```json
{
  "mcpServers": {
    "files": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/scratch"]
    },
    "remote": {
      "url": "https://example.com/mcp",
      "headers": { "Authorization": "Bearer ..." }
    }
  }
}
```

An entry needs **either** `command` (a local stdio process) **or** `url` (Streamable
HTTP) — never both, never neither — and unknown keys are rejected rather than quietly
producing a server with no transport. Server names must match `^[A-Za-z0-9_-]{1,64}$`,
because the name becomes part of every tool name. `"enabled": false` parks a server
without deleting it.

Unlike the Anthropic API key, `env` and `headers` values come back from `GET
/api/mcp/servers` exactly as they were stored: the whole blob is edited in place, and a
config whose credentials had been replaced by `***` could not be saved again without
retyping them. They are still kept out of logs and error messages.

Nothing connects when you save. The first connect happens when you open the tool list,
press **Reconnect**, or start a chat turn — a server that is slow or broken can never
hold up boot, the health check, or a turn that does not use it. A server gets 10 s to
start and speak protocol and 60 s per tool call; past either it is marked `error` with
the reason, its tools disappear, and everything else keeps working.

Per-tool toggles live under **Show tools**. A disabled tool is not offered to the model
at all. Keep the total under about 40: large tool sets make models pick worse and the
definitions cost prompt tokens on every turn, so the panel warns above that.

### stdio servers run on your machine

A `command` server is spawned by the backend process, so it gets **your** machine: your
real filesystem, your `localhost`, your local databases and SSH agent. Paths in the
config are ordinary paths on this computer.

One thing is not inherited. Secrets must go in the entry's `env` block: the MCP SDK does
**not** hand the subprocess this app's environment — it gets an allow-list (`HOME`,
`LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER`) with `env` merged on top. So `npx` resolves
through `PATH`, but a server's API key only exists if you wrote it into `env`.

A cold `npx -y ...` downloads the package first, which can take longer than the 10 s
connect budget. If it trips, press **Reconnect** — the download has finished by then.

## Tests and linting

```sh
make test        # backend: uv run pytest, then frontend: npx vitest run
make lint        # backend: uv run ruff check ., then frontend: npm run lint (oxlint)
make typecheck   # frontend: npx tsc -b — the only TypeScript type-check there is
```

## Layout

| Path        | What it is                                              |
| ----------- | ------------------------------------------------------- |
| `backend/`  | FastAPI app (`app/`), tests, uv-managed dependencies     |
| `frontend/` | Vite + React + TypeScript SPA, built into `frontend/dist` |
| `docs/`     | `DESIGN.md` (design record), `ROADMAP.md` (backlog)       |
