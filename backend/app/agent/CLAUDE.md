# app/agent/ — the manual agent loop

Read `backend/CLAUDE.md` (app wiring, SSE table, test harness) and the root
`CLAUDE.md` (Anthropic API gotchas) first.

| File | What it holds |
|---|---|
| `runner.py` | `run(...)` — the loop. `sanitize_for_replay`, `parse_tool_input`, the server-tool result decoding. |
| `registry.py` | `ToolProvider` protocol, `RegisteredTool`, `ToolResult`, `ToolSource`, `ToolRegistry`, `sanitize_tool_name`. |
| `builtin.py` | `BuiltinToolProvider` (3 local tools) and `ServerToolProvider` (Anthropic web_search/web_fetch). |
| `providers.py` | `build_tool_providers(request, session, session_factory)` — the **only** place the provider list is built — and `turn_settings(session, app_settings)`, the **only** place a run's settings are read. |
| `persistence.py` | `Persistence` / `NullPersistence`, `load_history`, `repair_unanswered_tool_use`, `flatten_text`, `ToolCallRecord`. |
| `prompts.py` | `DEFAULT_SYSTEM_PROMPT`, `build_system_prompt(override=, extra=)`. |
| `events.py` | The frozen event dataclasses + `to_sse()`. |

**Why a manual loop and not `client.beta.messages.tool_runner`:** the Python tool runner
silently ends the loop on `pause_turn` (returns a truncated answer, no error), cannot be
resumed mid-loop, and gives no mid-turn persistence, custom event mapping or cancellation.

## `run(...)`

```python
async def run(
    *,
    client: AsyncAnthropic,
    db_session_factory: async_sessionmaker[AsyncSession],
    registry: ToolRegistry,
    session_id: int | None,
    user_content: list[dict[str, Any]],
    model: str, effort: str, thinking_display: str, max_tool_turns: int,
    system_override: str | None = None,
    system_extra: str = "",
    tool_subset: Collection[str] | None = None,
    max_pause_restarts: int = 5,
    persist: bool = True,
) -> AsyncIterator[ev.AgentEvent]: ...
```

Transport-agnostic: both streaming routes drive it through
`app/api/streaming.py::pump_agent_events` and map the events to SSE.

- `persist=False` → `NullPersistence`: identical loop, identical events, dispatch,
  `pause_turn`/refusal/fallback handling, **zero rows written**; `session_id` may then be
  `None`. `persist=True` with `session_id is None` raises `ValueError`. This is exactly
  what note generation uses (`app/api/notes.py`).
- `system_override` **replaces** `DEFAULT_SYSTEM_PROMPT`; `system_extra` (the
  `system_prompt_extra` setting) is appended in both cases.
- `tool_subset` filters `registry.tools()` by name; names no provider offers are logged
  and ignored (so a subset naming `web_search` still works when the toggle is off).
  Notes generation passes `{get_feed_item, fetch_article}` ± `web_search`.
- `max_pause_restarts` caps `pause_turn` resumes → `error(turn_limit)`.
- The generator is safe to `aclose()` at any point: `CancelledError`/`GeneratorExit` is
  logged and **re-raised**; whatever was committed stays committed.

Callers must not read settings themselves: `providers.py::turn_settings(session,
app_settings)` returns `api_key` / `model` / `effort` / `thinking_display` /
`max_tool_turns` / `system_prompt_extra` in one read, inside the request's own
transaction and **before** the stream opens, so a settings edit mid-run cannot shift
the prompt (and therefore the cache prefix) under a model that is already answering.
`api_key` follows the env → `.env` → stored precedence in
`app/services/settings.py::get_effective_api_key`.

Module constants: `FALLBACK_BETA = "server-side-fallback-2026-07-01"`,
`FALLBACKS = "default"`, `MAX_TOKENS = 64_000`, `PREVIEW_CHARS = 600`.

## The request the loop builds

Every iteration sends `model`, `max_tokens=MAX_TOKENS`, `betas=[FALLBACK_BETA]`,
`fallbacks=FALLBACKS`, `thinking={"type":"adaptive","display":thinking_display}`,
`output_config={"effort": effort}`, `cache_control={"type":"ephemeral"}` (top level),
`system`, `messages` (a **snapshot** `list(messages)`, never the live list), plus `tools`
when non-empty and `container` when one is held.

## `stop_reason` handling

| `stop_reason` | What the loop does |
|---|---|
| `end_turn` | break, clean finish |
| `refusal` | `error(refusal, category=stop_details.category)` then `done`. **Content is only read after `stop_reason` is inspected** — a refusal's `content` is `[]`, and the assistant row is persisted with that empty list plus `stop_details` inside `usage_json` (there is no column for it). |
| `max_tokens` | `error(max_tokens)` |
| `pause_turn` | append the paused assistant turn (`sanitize_for_replay(content)`) to the **full** history, **no injected user message**, re-request. Past `max_pause_restarts` → `error(turn_limit)`. |
| `tool_use` | dispatch (below). Past `max_tool_turns` → synthesise `is_error` tool results for the unanswered blocks (so the transcript stays replayable) then `error(turn_limit)`. |
| anything else | `error(api_error, "Unexpected stop_reason: ...")` |

Errors: `RateLimitError → error(rate_limit)`, `APIConnectionError → error(connection)`,
`APIStatusError → error(api_error, status=...)`. **One retry only**, and only to drop a
rejected `container` id (`_is_container_rejection`: a 400 mentioning "container"); that
400 is raised before any content streams, so the retry cannot duplicate events. A bare
`except Exception` wraps the whole body — including setup (`registry.tools()`,
`load_history`, the first commit) — because a generator that dies without `error`+`done`
strands the browser on a truncated stream.

## Tool dispatch

All `tool_use` blocks of a turn are dispatched with `asyncio.gather(...,
return_exceptions=True)`; an exception that escapes becomes `ToolResult(is_error=True)`
(except `CancelledError`, which is re-raised). **Every result block goes into ONE user
message.** Each result is persisted (`tool_call_result`) and emitted as a `tool_result`
event with a ≤600-char preview.

## Container threading

`web_search_20260209` / `web_fetch_20260209` run code execution server-side, which
allocates a container. `container_id` is **turn-scoped**: captured from
`final.container.id`, sent on every continuation request of the same turn, recorded in
`usage_json`, and never carried across user turns (the server owns its lifetime).
Without it the API 400s with "container_id is required when there are pending tool uses".

`_SERVER_RESULT_TYPES` therefore includes `code_execution_tool_result`,
`bash_code_execution_tool_result` and `text_editor_code_execution_tool_result` as well as
the web ones — they are server tools we never declared, and dropping them orphans their
`server_tool_use`. `_server_tool_result_payload` decides failure with
`_is_server_tool_error` (type ends in `_tool_result_error` **or** `error_code` is set)
**before** any success branch — matching success type *prefixes* is the bug this replaced
(`text_editor_code_execution_tool_result_error` starts with `text_editor_code_execution`).

## Event mapping (`_stream_turn`)

`open_tool_blocks` maps a content-block **index** to its `tool_use_id`, and it registers
`tool_use` **and `server_tool_use`** blocks. Both kinds stream their input the same way:
when the model composes the argument, `content_block_start` carries `input == {}` and the
real value only exists in the `input_json_delta` fragments, so a `server_tool_use` block
that is not registered leaves the live card showing a web_search with no query and a code
execution with `{}` — until a reload, when the stored transcript (from
`get_final_message()`) answers instead. The fragments go out on the same
`tool_use_input` event for both, because the consumer patches by `tool_use_id`.

`ServerToolUse.input` stays the block's *initial* dict on purpose: it is the whole input
when the server filled it in, and `{}` when it is streaming — the fragments follow either
way, and nothing downstream may treat that `{}` as "no input".

## Persistence strategy

`Persistence(factory, session_id)` / `NullPersistence()` expose the same methods
(`load_history`, `user_message`, `assistant_message`, `tool_result_message`, `tool_calls`,
`tool_call_result`, `server_tool_result`, `usage`, `.message_ids`), so the loop body has
no `if persist:`.

Rules:

- **Never hold a DB transaction across an LLM call.** Every helper opens its own session,
  writes, commits, closes. Writes happen *as the turn progresses*, so a mid-turn browser
  refresh reloads a coherent (if incomplete) conversation.
- `messages.content_json` stores the **unsanitised** content — the DB is the transcript of
  record; sanitisation is request-time only.
- `seq` is allocated as `max(seq)+1` *inside* the write transaction, backed by
  `UNIQUE(session_id, seq)`, so concurrent writers collide instead of overwriting.
- Session usage totals add `input_tokens + cache_read_input_tokens +
  cache_creation_input_tokens` — with `cache_control` on, `input_tokens` alone is only the
  uncached remainder and would understate a long session by orders of magnitude.
- A server tool's result often lands in a *later* assistant message than its
  `server_tool_use`, so `record_server_tool_result` looks the row up by `tool_use_id`
  **across the session**, not within one message.

### `repair_unanswered_tool_use` and `sanitize_for_replay`

`load_history` reads `content_json` back in `seq` order, runs each assistant turn through
`sanitize_for_replay`, then runs the whole list through `repair_unanswered_tool_use`.

- **`repair_unanswered_tool_use`** answers every `tool_use` the stored transcript left
  hanging (turn cap fired, user hit Stop, process killed mid-`gather`) with a synthesised
  `tool_result` `{"content": "tool call was interrupted", "is_error": true}`. Without it
  the replay sends `assistant[tool_use]` followed by `user[text]`, which the API rejects —
  and since the transcript is append-only the session would 400 forever. Synthesis, not
  dropping: dropping would also take the turn's thinking and text. Results merge into the
  following user message when there is one, else become a new trailing user message; the
  runner then merges that trailing message with the new user content so roles stay
  strictly alternating. **Only the user's own content is persisted for the new turn** —
  repair blocks belong to the interrupted turn.
- **`sanitize_for_replay`** is the **identity when no `fallback` block is present** (the
  normal case — `final.content` echoes back verbatim, thinking signatures and all). When a
  mid-output fallback happened, everything *before the last `fallback` block* that is
  `thinking` / `redacted_thinking` / `tool_use`, a `server_tool_use` without its matching
  result, or an unrecognised block type, is dropped; text blocks, paired server-tool
  blocks and everything after the boundary echo normally; the `fallback` block itself is
  an audit marker and is always dropped.

## The tool registry

`ToolProvider` (runtime-checkable Protocol): a `source: ToolSource` attribute and
`async def list_tools() -> list[RegisteredTool]`.

`RegisteredTool(name, source, definition, handler=None, server_name=None)` — `handler` is
`None` for server tools (Anthropic runs them). `ToolResult(content: str, is_error=False,
raw=None)` — `content` is what the model reads, `raw` is what lands in
`tool_calls.result_json`.

`ToolRegistry(providers, timeout_s=DEFAULT_TOOL_TIMEOUT_S=60.0)`:

- **Lists providers at most once per registry** (`_collect`, lock-guarded, cached). A turn
  with four parallel calls must not re-enumerate an MCP server four times, and re-listing
  mid-turn could change the tool set under the conversation's prompt cache. Build one
  registry per turn.
- **Stable ordering is load-bearing** — the `tools` array is the head of the prompt-cache
  prefix, so a reorder invalidates the cache for the whole conversation. Order is provider
  order (builtin → server → mcp), and within a provider deterministic (name-ascending for
  builtin and MCP; a fixed literal order for server tools so toggling one off and on does
  not move the other).
- Names are coerced through `sanitize_tool_name` (`^[a-zA-Z0-9_-]{1,128}$`, other chars →
  `_`); duplicates are dropped with a warning, first registration wins.
- `dispatch(name, input)` **never raises**: unknown/handler-less name, `asyncio.timeout`
  (60 s), `TypeError` (the model invented an argument), or any exception → a
  `ToolResult(is_error=True)`. Only `CancelledError` propagates. Dropping a result for a
  `tool_use` block is a protocol violation, and raising would abort a recoverable turn.

## Built-in and server tools

`BuiltinToolProvider(db_session_factory, settings_service=...)` — `search_feed_items`
(the inbox, `DEFAULT_SEARCH_LIMIT` 20 / `MAX_SEARCH_LIMIT` 50 results,
`MAX_SEARCH_CHARS` 8 000 rendered), `get_feed_item` (full text, extracting on demand),
`fetch_article` (a URL not in the inbox, `DEFAULT_ARTICLE_CHARS` 12 000). Each handler
opens **its own short transaction** and commits before returning. `fetch_article` goes
through `extract_service.extract_article` → `url_guard.fetch_guarded(validate_first_hop=True)`;
there must never be a second HTTP path around the guard.

`search_feed_items` delegates to `items_service.list_items`, whose `q` filter matches
`title`, `summary` **and `content_text`** — so a CVE that only appears in an extracted
article body is findable, and the model's view of the inbox agrees with the Inbox page
and the global search. All three go through `app/db/util.py::matches`.

Tool **descriptions are prescriptive about *when* to call** ("Call this FIRST for any
question about recent security news…"). Recent Opus models reach for tools conservatively
and those trigger conditions move the should-call rate — do not rewrite them into neutral
summaries.

`ServerToolProvider(web_search_enabled, web_search_max_uses, web_fetch_enabled)` —
built via `await ServerToolProvider.from_settings(session)`, a **snapshot** read once at
turn start (re-reading per call would change the tools array mid-conversation). Emits
`{"type": "web_search_20260209", "name": "web_search", "max_uses": N}` and
`{"type": "web_fetch_20260209", "name": "web_fetch"}`, `handler=None`. Both off → the
model is restricted to the local inbox.

`providers.py::build_tool_providers(request, session, session_factory)` is the single
place the list is assembled (chat and notes generation must not offer different tools;
they differ by `tool_subset`, not by provider). It reads settings, the MCP server set
and the tool prefs from the request's own transaction, calls `sync_manager`, and
**connects nothing** — MCP is best-effort, an unreachable server contributes no tools
rather than an error. `session_factory` is passed explicitly rather than read off
`app.state` so a test overriding `get_session_factory` really redirects the built-ins'
own transactions.

## Pitfalls specific to this package

1. Do not reorder providers or tools, do not put a timestamp in the system prompt: both
   destroy the prompt cache for every existing conversation.
2. Do not add a second timeout inside a tool handler — `dispatch` already applies 60 s.
3. Do not read `content` before `stop_reason`, and do not assume a server-tool result's
   `content` is a list.
4. Do not split parallel tool results across user messages.
5. Do not rebuild the assistant turn by hand when appending it to history — pass
   `sanitize_for_replay(content_dicts)`, which is the identity in the normal case.
6. Do not let an exception escape the generator; the consumer is owed `error` + `done`.
7. `parse_tool_input` is for tests/diagnostics only — the loop reads the parsed `input`
   dict off `get_final_message()`, never the `input_json_delta` fragments.
8. `events.py` and the SSE table in `backend/CLAUDE.md` are the same contract stated
   twice; change both together. The *terminal* contract is not this package's — chat
   ends on `done`, a note generation ends on `done` only when the note was saved. The
   route decides, not the runner.
9. Do not read a setting inside the loop or a tool handler. `turn_settings` reads them
   once per run, before the stream opens.
