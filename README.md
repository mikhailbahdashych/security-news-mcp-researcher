# Security News MCP Researcher

A local-only, single-user web app for security engineers: an RSS security-news inbox,
an LLM research chat, and a meeting-notes generator. FastAPI backend, React SPA,
SQLite storage — nothing leaves your machine except the calls you ask it to make.

## Quickstart (Docker)

```sh
make up          # docker compose up --build
```

Then open <http://localhost:8000>. Data lives in the named `appdata` volume, so it
survives `docker compose down`.

## Local development

Two terminals:

```sh
make dev-api     # FastAPI with reload on :8000
make dev-web     # Vite dev server on :5173, proxying /api to :8000
```

Then open <http://localhost:5173>.

Prerequisites: [uv](https://docs.astral.sh/uv/) and Node 22+.
Copy `.env.example` to `.env` if you want to override defaults.

## The inbox

Feeds are pulled on demand — press **Refresh feeds** — and each entry is deduplicated
on its feed's own guid (falling back to the entry link, then to a hash of title and
link), so refreshing twice adds nothing. Headlines are triaged star / dismiss, and
**Extract article** fetches the page behind an item and stores its text as markdown;
a paywalled or JavaScript-only page is reported rather than stored, and the feed
summary stays as the fallback. **Seed defaults** adds a starter set of security
sources (The Hacker News, BleepingComputer, Krebs on Security, CISA advisories, SANS
ISC, Google Project Zero).

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

## MCP servers

Settings → **MCP servers** takes the same `mcpServers` JSON you would paste into Claude
Desktop. Saved servers' tools become callable from the research chat, namespaced
`mcp__{server}__{tool}`, alongside the built-in inbox tools and Anthropic's web search.

```json
{
  "mcpServers": {
    "files": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data/scratch"]
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

Nothing connects when you save. The first connect happens when you open the tool list,
press **Reconnect**, or start a chat turn — a server that is slow or broken can never
hold up boot, the health check, or a turn that does not use it. A server gets 10 s to
start and speak protocol and 60 s per tool call; past either it is marked `error` with
the reason, its tools disappear, and everything else keeps working.

Per-tool toggles live under **Show tools**. A disabled tool is not offered to the model
at all. Keep the total under about 40: large tool sets make models pick worse and the
definitions cost prompt tokens on every turn, so the panel warns above that.

### stdio servers run inside the container

In Docker, a `command` server is spawned **inside the container's namespace**:

- Paths are container paths. `/data` is the mounted volume — put scratch directories
  there (`/data/scratch`), not on your Mac. Your home directory is not reachable.
- `localhost` is the container, not your machine. A service on your host is not
  reachable at `http://localhost:...` from a stdio server started in here.
- Secrets must go in the entry's `env` block. The MCP SDK does **not** hand the
  subprocess this app's environment — it gets an allow-list (`HOME`, `LOGNAME`, `PATH`,
  `SHELL`, `TERM`, `USER`) with `env` merged on top. So `npx` resolves through `PATH`,
  but a server's API key only exists if you wrote it into `env`.
- The first `npx -y ...` downloads the package inside the container, which can take
  longer than the 10 s connect budget on a cold cache. If it trips, press **Reconnect** —
  the download has finished by then. The cache lives on the `/data` volume, so it
  survives a rebuild and only the very first run is slow.

**The escape hatch**: if a server genuinely needs your host — your real filesystem, a
local database, an SSH agent — run the backend on the host instead:

```sh
make dev-api     # same SQLite file, same UI, no container namespace
```

`url`-transport servers behave identically in both modes and are the better choice for
anything remote.

## Tests and linting

```sh
make test        # cd backend && uv run pytest
make lint        # cd backend && uv run ruff check .
```

## Layout

| Path        | What it is                                              |
| ----------- | ------------------------------------------------------- |
| `backend/`  | FastAPI app (`app/`), tests, uv-managed dependencies     |
| `frontend/` | Vite + React + TypeScript SPA, built into `frontend/dist` |
| `docs/`     | Design notes                                             |

In Docker the SPA is built and served by the backend from `/app/static`, so the whole
app is one container on one port.
