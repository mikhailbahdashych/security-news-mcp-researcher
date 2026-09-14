"""The manual agent loop.

**Why manual and not ``client.beta.messages.tool_runner``.** The Python tool runner
does not auto-resume ``pause_turn``: a paused turn silently ends the loop and is
returned as the final message — no error, no warning, just a truncated answer. It
also cannot be resumed mid-loop, and we need three more things it does not offer:
mid-turn persistence, a custom event mapping for the UI, and cancellation.

**Why the beta namespace.** We use server-side refusal fallbacks
(``fallbacks="default"``), which is a beta, and that puts the whole chat path on
``client.beta.messages`` with ``Beta*`` block types.

Two rules run through the whole file:

* Never hold a database transaction open across an LLM call. Each persistence step
  opens its own session, writes and commits before the next network call starts.
* Always check ``stop_reason`` before reading ``content``. A refusal is a normal
  HTTP 200 whose ``content`` can be an empty list, and this app's entire domain is
  security content, so a refusal is a first-class state rather than an edge case.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Collection
from typing import Any

import anthropic
from anthropic import AsyncAnthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import events as ev
from app.agent.persistence import (
    INTERRUPTED_TOOL_RESULT,
    NullPersistence,
    Persistence,
    ToolCallRecord,
)
from app.agent.prompts import build_system_prompt
from app.agent.registry import RegisteredTool, ToolRegistry, ToolResult, ToolSource

logger = logging.getLogger(__name__)

#: The beta that gates the ``fallbacks: "default"`` scalar form. The array form
#: uses ``server-side-fallback-2026-06-01``; pairing either header with the other
#: form is a 400, so this constant and ``FALLBACKS`` move together or not at all.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACKS = "default"

#: Thinking and text share this cap, so it is sized for a long researched answer.
MAX_TOKENS = 64_000

#: How much of a tool result the UI event carries.
PREVIEW_CHARS = 600

#: Block types that are model-internal and must not be echoed back across a
#: mid-output fallback boundary.
_INTERNAL_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking", "tool_use"})

#: Every result block a server tool can produce. The web_search/web_fetch
#: `_20260209` variants run code execution under the hood, so a turn that
#: searches the web also emits the code-execution result types — they are server
#: tools we never declared, and dropping them orphans their server_tool_use.
_SERVER_RESULT_TYPES = frozenset(
    {
        "web_search_tool_result",
        "web_fetch_tool_result",
        "code_execution_tool_result",
        "bash_code_execution_tool_result",
        "text_editor_code_execution_tool_result",
    }
)

#: Block types we recognise as safe to echo (anything else before a fallback
#: boundary is treated as model-internal and dropped).
_KNOWN_BLOCK_TYPES = (
    frozenset(
        {
            "text",
            "thinking",
            "redacted_thinking",
            "tool_use",
            "server_tool_use",
            "fallback",
        }
    )
    | _SERVER_RESULT_TYPES
)

#: Result block type -> the tool name to show when the matching server_tool_use
#: block arrived in an earlier assistant message and is not in hand.
_SERVER_RESULT_NAMES = {
    "web_search_tool_result": "web_search",
    "web_fetch_tool_result": "web_fetch",
    "code_execution_tool_result": "code_execution",
    "bash_code_execution_tool_result": "bash_code_execution",
    "text_editor_code_execution_tool_result": "text_editor_code_execution",
}


def _block_type(block: Any) -> str | None:
    if isinstance(block, dict):
        value = block.get("type")
        return value if isinstance(value, str) else None
    return getattr(block, "type", None)


def _block_id(block: Any) -> str | None:
    if isinstance(block, dict):
        return block.get("id")
    return getattr(block, "id", None)


def _tool_use_id(block: Any) -> str | None:
    if isinstance(block, dict):
        return block.get("tool_use_id")
    return getattr(block, "tool_use_id", None)


def _to_dict(block: Any) -> Any:
    """Serialise an SDK block to a plain dict via the SDK's own model_dump."""
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return block


def sanitize_for_replay(content: Any) -> list[Any]:
    """Strip the blocks that must not be echoed back after a mid-output fallback.

    With ``fallbacks`` enabled a turn can switch models mid-output. When that turn
    is echoed back on the next request, every ``thinking`` / ``redacted_thinking``
    / ``tool_use`` block — plus any ``server_tool_use`` block without its matching
    result, and any block type we do not recognise — that appears **before the
    final ``fallback`` block** must be omitted. Text blocks, paired server-tool
    blocks and everything after the boundary echo normally; the ``fallback`` block
    itself is an ignored audit marker.

    With no ``fallback`` block present (the overwhelming majority of turns) this is
    the identity function, which is exactly what the thinking-block replay rules
    require: append ``final.content`` verbatim, never a reconstructed version.
    """
    if not isinstance(content, list):
        return content

    boundary = -1
    for index, block in enumerate(content):
        if _block_type(block) == "fallback":
            boundary = index
    if boundary < 0:
        return list(content)

    paired_server_tool_ids = {
        _tool_use_id(block) for block in content if _block_type(block) in _SERVER_RESULT_TYPES
    }

    kept: list[Any] = []
    for index, block in enumerate(content):
        kind = _block_type(block)
        if kind == "fallback":
            continue
        if index > boundary:
            kept.append(block)
            continue
        if kind in _INTERNAL_BLOCK_TYPES or kind not in _KNOWN_BLOCK_TYPES:
            continue
        if kind == "server_tool_use" and _block_id(block) not in paired_server_tool_ids:
            continue
        kept.append(block)
    return kept


def _usage_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    dumped = _to_dict(usage)
    if not isinstance(dumped, dict):
        return {}
    return {
        "input_tokens": dumped.get("input_tokens") or 0,
        "output_tokens": dumped.get("output_tokens") or 0,
        "cache_read_input_tokens": dumped.get("cache_read_input_tokens") or 0,
        "cache_creation_input_tokens": dumped.get("cache_creation_input_tokens") or 0,
    }


#: Small scalars worth surfacing from a text-editor result. The result block can
#: also carry the file's whole body; that is not something the UI wants in a card.
_TEXT_EDITOR_SUMMARY_FIELDS = (
    "file_type",
    "num_lines",
    "total_lines",
    "start_line",
    "is_file_update",
    "old_lines",
    "new_lines",
)


def _is_server_tool_error(kind: str, content: dict[str, Any]) -> bool:
    """Is this result object a failure?

    Two independent signals, because neither alone is reliable across the family:
    every SDK error block's ``type`` ends in ``_tool_result_error``, and every one
    carries ``error_code``. Matching on the *success* type prefixes instead is the
    bug this replaced — ``text_editor_code_execution_tool_result_error`` starts
    with ``text_editor_code_execution`` and was being read as a success.
    """
    return kind.endswith("_tool_result_error") or content.get("error_code") is not None


def _server_tool_result_payload(block: Any) -> tuple[bool, Any]:
    """Turn a server-tool result block into ``(is_error, payload)``.

    Server-tool errors do not raise: they come back HTTP 200 with an *object* in
    ``content`` where a *list* would otherwise be. Branch on that before indexing.
    An object is not on its own a failure, though — a successful code-execution or
    text-editor result is an object too — so failure is decided by
    :func:`_is_server_tool_error`, and that test runs *before* any success branch.
    """
    dumped = _to_dict(block)
    content = dumped.get("content") if isinstance(dumped, dict) else None

    if isinstance(content, list):
        results = []
        for entry in content:
            if isinstance(entry, dict):
                results.append({"title": entry.get("title"), "url": entry.get("url")})
        return False, results

    if isinstance(content, dict):
        kind = content.get("type") or ""

        if _is_server_tool_error(kind, content):
            return True, {
                "type": kind,
                "error_code": content.get("error_code"),
                # Only the text-editor errors carry one; null elsewhere.
                "error_message": content.get("error_message"),
            }

        if kind == "web_fetch_result":
            return False, {"url": content.get("url"), "retrieved_at": content.get("retrieved_at")}
        if kind.endswith("code_execution_result"):
            return_code = content.get("return_code")
            return bool(return_code), {
                "stdout": content.get("stdout"),
                "stderr": content.get("stderr"),
                "return_code": return_code,
            }
        if kind.startswith("text_editor_code_execution"):
            summary: dict[str, Any] = {"type": kind}
            summary.update(
                {
                    field: content[field]
                    for field in _TEXT_EDITOR_SUMMARY_FIELDS
                    if content.get(field) is not None
                }
            )
            return False, summary
        return True, content

    return False, content


async def _stream_turn(
    stream: Any, registry_names: dict[str, RegisteredTool]
) -> AsyncIterator[ev.AgentEvent]:
    """Map raw stream events onto our event vocabulary.

    Tool input arrives as ``input_json_delta`` fragments that are only valid JSON
    once concatenated; they are forwarded raw for display and never parsed here.
    Execution reads the parsed ``input`` dict off ``get_final_message()``.
    """
    open_tool_blocks: dict[int, str] = {}
    #: server_tool_use id -> name, so a result block can be labelled with the
    #: tool that actually produced it rather than a guess from its own type.
    server_tool_names: dict[str, str] = {}

    async for event in stream:
        kind = getattr(event, "type", None)

        if kind == "content_block_start":
            block = event.content_block
            block_type = getattr(block, "type", None)
            if block_type == "tool_use":
                tool = registry_names.get(block.name)
                source = tool.source.value if tool else ToolSource.BUILTIN.value
                open_tool_blocks[event.index] = block.id
                yield ev.ToolUseStart(tool_use_id=block.id, name=block.name, source=source)
            elif block_type == "server_tool_use":
                raw_input = getattr(block, "input", None)
                server_tool_names[block.id] = block.name
                yield ev.ServerToolUse(
                    tool_use_id=block.id,
                    name=block.name,
                    input=raw_input if isinstance(raw_input, dict) else {},
                )
            elif block_type in _SERVER_RESULT_TYPES:
                is_error, payload = _server_tool_result_payload(block)
                tool_use_id = _tool_use_id(block) or ""
                yield ev.ServerToolResult(
                    tool_use_id=tool_use_id,
                    # The matching use may have arrived in an earlier assistant
                    # message, so fall back to the result type's own name.
                    name=server_tool_names.get(
                        tool_use_id, _SERVER_RESULT_NAMES.get(block_type, block_type)
                    ),
                    is_error=is_error,
                    results=payload,
                )

        elif kind == "content_block_delta":
            delta = event.delta
            delta_type = getattr(delta, "type", None)
            if delta_type == "text_delta":
                yield ev.TextDelta(text=delta.text)
            elif delta_type == "thinking_delta":
                yield ev.ThinkingDelta(text=delta.thinking)
            elif delta_type == "input_json_delta":
                tool_use_id = open_tool_blocks.get(event.index)
                if tool_use_id is not None:
                    yield ev.ToolUseInput(
                        tool_use_id=tool_use_id, partial_json=delta.partial_json or ""
                    )

        elif kind == "content_block_stop":
            open_tool_blocks.pop(event.index, None)


async def _dispatch_one(
    registry: ToolRegistry, name: str, tool_input: dict[str, Any]
) -> tuple[ToolResult, int]:
    started = time.monotonic()
    result = await registry.dispatch(name, tool_input)
    return result, int((time.monotonic() - started) * 1000)


async def run(
    *,
    client: AsyncAnthropic,
    db_session_factory: async_sessionmaker[AsyncSession],
    registry: ToolRegistry,
    session_id: int | None,
    user_content: list[dict[str, Any]],
    model: str,
    effort: str,
    thinking_display: str,
    max_tool_turns: int,
    system_override: str | None = None,
    system_extra: str = "",
    tool_subset: Collection[str] | None = None,
    max_pause_restarts: int = 5,
    persist: bool = True,
) -> AsyncIterator[ev.AgentEvent]:
    """Run one user turn to completion, yielding events as it goes.

    Transport-agnostic on purpose: the API layer turns these events into SSE and
    Task 6's note generation consumes the same generator directly.

    ``persist=False`` runs an identical loop — same events, same tool dispatch,
    same ``pause_turn`` and refusal handling — while writing no rows at all, which
    is what note generation needs.

    Safe to ``aclose()`` at any point: whatever has already been committed stays
    committed, a terminal ``cancelled`` error plus ``done`` are emitted if the
    consumer is still listening, and ``CancelledError`` is re-raised.
    """
    if persist and session_id is None:
        raise ValueError("session_id is required when persist=True")

    store: Persistence = (
        Persistence(db_session_factory, session_id)  # type: ignore[arg-type]
        if persist
        else NullPersistence()
    )

    turn = 0
    pause_restarts = 0
    terminal: ev.Error | None = None
    #: Turn-scoped. web_search/web_fetch `_20260209` run code execution under the
    #: hood, which allocates a container; every continuation request of the same
    #: turn has to name it or the API rejects the pending tool uses with a 400.
    #: It is deliberately not carried across turns — the server owns its lifetime.
    container_id: str | None = None

    # Everything from here down is inside the safety net, including the setup:
    # listing tools, loading history (which runs the replay repair) and the first
    # commit can all fail, and an exception escaping the generator would leave
    # the consumer on a stream with no `error` and no `done`.
    try:
        tools = await registry.tools(subset=tool_subset)
        tool_definitions = [tool.definition for tool in tools]
        by_name = {tool.name: tool for tool in tools}
        system_prompt = build_system_prompt(override=system_override, extra=system_extra)

        messages = await store.load_history()
        if messages and messages[-1]["role"] == "user":
            # load_history's repair can leave a synthesised tool_result message at
            # the end; merging keeps user/assistant strictly alternating rather
            # than relying on the API to coalesce two user turns.
            messages[-1]["content"] = list(messages[-1]["content"]) + list(user_content)
        else:
            messages.append({"role": "user", "content": user_content})
        # What is *persisted* is only ever the user's own content — the repair
        # blocks belong to the interrupted turn, not to this one.
        await store.user_message(user_content)

        while True:
            yield ev.TurnStart(turn=turn)

            request: dict[str, Any] = {
                "model": model,
                "max_tokens": MAX_TOKENS,
                "betas": [FALLBACK_BETA],
                "fallbacks": FALLBACKS,
                "thinking": {"type": "adaptive", "display": thinking_display},
                "output_config": {"effort": effort},
                # Auto-caches the last cacheable block, so the tools array, the
                # system prompt and the transcript so far are a cache read on the
                # next request instead of full-price input. The whole prefix is
                # kept byte-stable for exactly this reason.
                "cache_control": {"type": "ephemeral"},
                "system": system_prompt,
                # A snapshot: the loop keeps appending to `messages`, and handing
                # the live list to the SDK would let a later turn mutate a request
                # that is still in flight.
                "messages": list(messages),
            }
            if tool_definitions:
                request["tools"] = tool_definitions
            if container_id is not None:
                request["container"] = container_id

            final = None
            # At most two attempts, and only ever a second one to drop a container
            # id the server has stopped accepting. A container rejection is a 400
            # raised before any content streams, so the retry cannot duplicate
            # events the consumer has already seen.
            for attempt in range(2):
                sent_container = request.get("container")
                try:
                    async with client.beta.messages.stream(**request) as stream:
                        async for event in _stream_turn(stream, by_name):
                            yield event
                        final = await stream.get_final_message()
                    break
                except anthropic.RateLimitError as exc:
                    terminal = ev.Error(error_type="rate_limit", message=str(exc))
                    break
                except anthropic.APIStatusError as exc:
                    if attempt == 0 and sent_container and _is_container_rejection(exc):
                        logger.warning(
                            "Container %s was rejected; retrying the request without it",
                            sent_container,
                        )
                        request.pop("container", None)
                        container_id = None
                        continue
                    terminal = ev.Error(
                        error_type="api_error", message=str(exc), status=exc.status_code
                    )
                    break
                except anthropic.APIConnectionError as exc:
                    terminal = ev.Error(error_type="connection", message=str(exc))
                    break

            if terminal is not None:
                break
            if final is None:
                terminal = ev.Error(
                    error_type="api_error", message="The model returned no message."
                )
                break

            # The container belongs to this turn: every continuation request has
            # to name it, or the API rejects the pending tool uses with
            # "container_id is required when there are pending tool uses".
            container = getattr(final, "container", None)
            if container is not None and getattr(container, "id", None):
                container_id = container.id

            stop_reason = getattr(final, "stop_reason", None)
            usage = _usage_dict(getattr(final, "usage", None))
            # Only read content once stop_reason has been seen — a refusal's
            # content can be an empty list.
            content_dicts = [_to_dict(block) for block in (getattr(final, "content", None) or [])]

            # `messages` has no column for stop_details, and a refused turn
            # persists an EMPTY content list — so stop_reason plus this is
            # everything the UI has to render a refusal from after a reload.
            # usage_json is the turn's metadata blob; nothing reads it back into
            # a request.
            details = getattr(final, "stop_details", None)
            turn_meta: dict[str, Any] = dict(usage)
            if details is not None:
                turn_meta["stop_details"] = _to_dict(details)
            if container_id is not None:
                turn_meta["container_id"] = container_id

            assistant_message_id = await store.assistant_message(
                content_dicts, stop_reason, turn_meta or None
            )
            tool_use_blocks = [
                block
                for block in (getattr(final, "content", None) or [])
                if _block_type(block) == "tool_use"
            ]
            server_tool_blocks = [
                block
                for block in (getattr(final, "content", None) or [])
                if _block_type(block) == "server_tool_use"
            ]
            pending_records = [
                ToolCallRecord(
                    tool_use_id=block.id,
                    name=block.name,
                    source=_source_of(by_name, block.name),
                    server_name=_server_name_of(by_name, block.name),
                    input_json=_to_dict(getattr(block, "input", None)),
                )
                for block in tool_use_blocks
            ] + [
                ToolCallRecord(
                    tool_use_id=block.id,
                    name=block.name,
                    source=ToolSource.SERVER.value,
                    input_json=_to_dict(getattr(block, "input", None)),
                    result_json=_server_result_for(final, block),
                )
                for block in server_tool_blocks
            ]
            await store.tool_calls(assistant_message_id, pending_records)
            # A server tool's result often lands in a *later* assistant message
            # than its server_tool_use — code execution especially — so fill the
            # row by tool_use_id across the session rather than by message.
            for block in getattr(final, "content", None) or []:
                if _block_type(block) not in _SERVER_RESULT_TYPES:
                    continue
                result_is_error, _payload = _server_tool_result_payload(block)
                await store.server_tool_result(
                    _tool_use_id(block) or "",
                    result_json=_to_dict(block),
                    is_error=result_is_error,
                )
            # Count the cached tokens too. With cache_control on, `input_tokens`
            # is only the *uncached* remainder — on a long cached session it
            # reads as single digits — so the session totals would understate
            # the turn by orders of magnitude if they used it alone.
            await store.usage(
                usage.get("input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0),
                usage.get("output_tokens", 0),
            )

            yield ev.TurnEnd(turn=turn, stop_reason=stop_reason, usage=usage)

            if stop_reason == "refusal":
                terminal = ev.Error(
                    error_type="refusal",
                    message=(getattr(details, "explanation", None) or "")
                    or "The model declined this request.",
                    category=getattr(details, "category", None),
                )
                break

            if stop_reason == "end_turn":
                break

            if stop_reason == "max_tokens":
                terminal = ev.Error(
                    error_type="max_tokens",
                    message="The response hit the output limit before finishing.",
                )
                break

            if stop_reason == "pause_turn":
                pause_restarts += 1
                if pause_restarts > max_pause_restarts:
                    terminal = ev.Error(
                        error_type="turn_limit",
                        message=f"The turn paused more than {max_pause_restarts} times.",
                    )
                    break
                # Re-send with the paused assistant turn appended and NO injected
                # user message: the API detects the trailing server_tool_use block
                # and resumes. A "Continue." message would corrupt the resume.
                messages.append(
                    {"role": "assistant", "content": sanitize_for_replay(content_dicts)}
                )
                continue

            if stop_reason == "tool_use":
                turn += 1
                if turn > max_tool_turns:
                    # The assistant turn with its tool_use blocks is already
                    # committed. Leaving it unanswered would make the stored
                    # transcript unreplayable, so close it out before stopping.
                    await _persist_interrupted_tool_results(
                        store, assistant_message_id, tool_use_blocks
                    )
                    terminal = ev.Error(
                        error_type="turn_limit",
                        message=f"Stopped after {max_tool_turns} tool turns.",
                    )
                    break

                messages.append(
                    {"role": "assistant", "content": sanitize_for_replay(content_dicts)}
                )

                outcomes = await asyncio.gather(
                    *(
                        _dispatch_one(
                            registry,
                            block.name,
                            getattr(block, "input", None) or {},
                        )
                        for block in tool_use_blocks
                    ),
                    return_exceptions=True,
                )

                result_blocks: list[dict[str, Any]] = []
                for block, outcome in zip(tool_use_blocks, outcomes, strict=True):
                    if isinstance(outcome, BaseException):
                        if isinstance(outcome, asyncio.CancelledError):
                            raise outcome
                        logger.exception(
                            "Tool %s raised out of gather", block.name, exc_info=outcome
                        )
                        result = ToolResult(content=f"Error: {outcome}", is_error=True)
                        duration_ms = 0
                    else:
                        result, duration_ms = outcome

                    result_block: dict[str, Any] = {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result.content,
                    }
                    if result.is_error:
                        result_block["is_error"] = True
                    result_blocks.append(result_block)

                    await store.tool_call_result(
                        assistant_message_id,
                        block.id,
                        result_json={"content": result.content, "raw": result.raw},
                        is_error=result.is_error,
                        duration_ms=duration_ms,
                    )
                    yield ev.ToolResult(
                        tool_use_id=block.id,
                        name=block.name,
                        is_error=result.is_error,
                        duration_ms=duration_ms,
                        preview=result.content[:PREVIEW_CHARS],
                    )

                if result_blocks:
                    await store.tool_result_message(result_blocks)
                    # ALL results in ONE user message: splitting them trains the
                    # model out of parallel tool calls.
                    messages.append({"role": "user", "content": result_blocks})
                continue

            terminal = ev.Error(
                error_type="api_error",
                message=f"Unexpected stop_reason: {stop_reason!r}",
            )
            break

    except (asyncio.CancelledError, GeneratorExit):
        # Whatever committed stays committed — that is the point of per-turn
        # commits. Re-raise so the task actually dies. The unanswered tool_use
        # blocks this may leave behind are repaired on the way back out, by
        # persistence.repair_unanswered_tool_use.
        logger.info("Agent turn cancelled for session %s", session_id)
        raise
    except Exception as exc:  # noqa: BLE001 - the generator owes its consumer a terminal event
        # Anything not already mapped above. Letting it escape would strand the
        # consumer: the SSE pump would end without `error` or `done`, and the
        # browser would sit on a truncated stream forever.
        logger.exception("Agent turn failed for session %s", session_id)
        terminal = ev.Error(error_type="api_error", message=f"{type(exc).__name__}: {exc}")

    if terminal is not None:
        yield terminal
    yield ev.Done(session_id=session_id, message_ids=list(store.message_ids))


def _is_container_rejection(exc: anthropic.APIStatusError) -> bool:
    """Is this 400 the API refusing the container id we sent?

    The id is server-owned and can expire between turns, so one retry without it
    is far cheaper than losing the turn's work.
    """
    return exc.status_code == 400 and "container" in str(exc).lower()


async def _persist_interrupted_tool_results(
    store: Persistence, assistant_message_id: int, tool_use_blocks: list[Any]
) -> None:
    """Close out tool_use blocks whose handlers never ran."""
    if not tool_use_blocks:
        return
    blocks = [
        {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": INTERRUPTED_TOOL_RESULT,
            "is_error": True,
        }
        for block in tool_use_blocks
    ]
    await store.tool_result_message(blocks)
    for block in tool_use_blocks:
        await store.tool_call_result(
            assistant_message_id,
            block.id,
            result_json={"content": INTERRUPTED_TOOL_RESULT, "raw": None},
            is_error=True,
            duration_ms=None,
        )


def _source_of(by_name: dict[str, RegisteredTool], name: str) -> str:
    tool = by_name.get(name)
    return tool.source.value if tool is not None else ToolSource.BUILTIN.value


def _server_name_of(by_name: dict[str, RegisteredTool], name: str) -> str | None:
    tool = by_name.get(name)
    return tool.server_name if tool is not None else None


def _server_result_for(final: Any, use_block: Any) -> Any:
    """Find the server-tool result matching a ``server_tool_use`` block, verbatim."""
    for block in getattr(final, "content", None) or []:
        kind = _block_type(block) or ""
        if kind.endswith("_tool_result") and _tool_use_id(block) == _block_id(use_block):
            return _to_dict(block)
    return None


def parse_tool_input(raw: str) -> dict[str, Any]:
    """Parse an accumulated ``input_json_delta`` buffer.

    Only used by tests and diagnostics — the loop reads the parsed dict off the
    final message. Opus 5 varies its JSON string escaping, so anything that needs
    the values must go through a real JSON parse, never string matching.
    """
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


__all__ = [
    "FALLBACKS",
    "FALLBACK_BETA",
    "MAX_TOKENS",
    "PREVIEW_CHARS",
    "parse_tool_input",
    "run",
    "sanitize_for_replay",
]
