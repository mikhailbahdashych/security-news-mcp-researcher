"""Stand-ins for ``anthropic.AsyncAnthropic``.

Two fakes live here, because they are two shapes of the same client.

``FakeAnthropicClient`` covers the settings/models endpoints:
``client.models.list()`` on the real async client is a *synchronous* call that
returns an auto-paginating async iterator, so that is exactly what it fakes —
including raising the error only once iteration starts.

``ScriptedAnthropic`` covers the chat path: ``client.beta.messages.stream(...)``
returns an async context manager that yields a scripted sequence of **real** SDK
event objects and whose ``get_final_message()`` returns a real ``BetaMessage``.
Real SDK types, not dicts or ``SimpleNamespace``, are what make these tests catch
shape drift — and the beta namespace means ``anthropic.types.beta.*``, because
``fallbacks`` is a beta and puts the whole chat path there.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import anthropic
import httpx2


@dataclass
class FakeModelInfo:
    id: str
    display_name: str
    created_at: datetime


class _FakePaginator:
    def __init__(self, items: list[FakeModelInfo], error: Exception | None) -> None:
        self._items = items
        self._error = error

    async def _iterate(self) -> AsyncIterator[FakeModelInfo]:
        if self._error is not None:
            raise self._error
        for item in self._items:
            yield item

    def __aiter__(self) -> AsyncIterator[FakeModelInfo]:
        return self._iterate()


class _FakeModelsResource:
    def __init__(self, items: list[FakeModelInfo], error: Exception | None) -> None:
        self.items = items
        self.error = error
        self.call_count = 0

    def list(self, **_kwargs: object) -> _FakePaginator:
        self.call_count += 1
        return _FakePaginator(self.items, self.error)


class FakeAnthropicClient:
    def __init__(
        self,
        items: list[FakeModelInfo] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.models = _FakeModelsResource(items or [], error)

    @property
    def call_count(self) -> int:
        return self.models.call_count


def api_error(status_code: int, message: str = "boom") -> anthropic.APIStatusError:
    """Build a real SDK exception for the given status code."""
    response = httpx2.Response(
        status_code,
        request=httpx2.Request("GET", "https://api.anthropic.com/v1/models"),
    )
    error_class = {
        400: anthropic.BadRequestError,
        401: anthropic.AuthenticationError,
        403: anthropic.PermissionDeniedError,
        404: anthropic.NotFoundError,
        429: anthropic.RateLimitError,
        500: anthropic.InternalServerError,
    }[status_code]
    return error_class(message, response=response, body=None)


def model(id_: str, display_name: str, year: int) -> FakeModelInfo:
    return FakeModelInfo(
        id=id_, display_name=display_name, created_at=datetime(year, 1, 1, tzinfo=UTC)
    )


# --------------------------------------------------------------------------
# The chat path: a scripted stream of real beta SDK objects.
# --------------------------------------------------------------------------

from typing import Any  # noqa: E402

from anthropic.types.beta import (  # noqa: E402
    BetaBashCodeExecutionResultBlock,
    BetaBashCodeExecutionToolResultBlock,
    BetaContainer,
    BetaInputJSONDelta,
    BetaMessage,
    BetaRawContentBlockDeltaEvent,
    BetaRawContentBlockStartEvent,
    BetaRawContentBlockStopEvent,
    BetaRawMessageStopEvent,
    BetaServerToolUseBlock,
    BetaTextBlock,
    BetaTextDelta,
    BetaThinkingBlock,
    BetaThinkingDelta,
    BetaToolUseBlock,
    BetaUsage,
)


def _message(
    content: list[Any],
    stop_reason: str,
    stop_details: Any = None,
    container_id: str | None = None,
) -> BetaMessage:
    return BetaMessage(
        id="msg_test",
        content=content,
        model="claude-opus-5",
        role="assistant",
        stop_reason=stop_reason,
        stop_details=stop_details,
        stop_sequence=None,
        type="message",
        usage=BetaUsage(input_tokens=11, output_tokens=7),
        # web_search/web_fetch `_20260209` run code execution under the hood and
        # allocate one of these; continuation requests have to name it.
        container=(
            None
            if container_id is None
            else BetaContainer(
                id=container_id,
                expires_at=datetime(2099, 1, 1, tzinfo=UTC),
                skills=[],
            )
        ),
    )


@dataclass
class ScriptedTurn:
    """One ``stream()`` call: the events it emits and the message it accumulates."""

    events: list[Any]
    message: BetaMessage
    delay_s: float = 0.0
    #: How long ``__aexit__`` takes. A real stream does not stop the instant it
    #: is cancelled — the runner still has commits in flight — and a caller that
    #: only *requests* cancellation returns before any of that has happened.
    teardown_s: float = 0.0


def _text_events(text: str, index: int = 0) -> list[Any]:
    return [
        BetaRawContentBlockStartEvent(
            type="content_block_start",
            index=index,
            content_block=BetaTextBlock(type="text", text=""),
        ),
        *(
            BetaRawContentBlockDeltaEvent(
                type="content_block_delta",
                index=index,
                delta=BetaTextDelta(type="text_delta", text=chunk),
            )
            for chunk in _chunks(text)
        ),
        BetaRawContentBlockStopEvent(type="content_block_stop", index=index),
    ]


def _chunks(text: str, size: int = 8) -> list[str]:
    """Split into several deltas so a test can see token-by-token streaming."""
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def turn_text(
    text: str,
    stop_reason: str = "end_turn",
    *,
    delay_s: float = 0.0,
    container_id: str | None = None,
) -> ScriptedTurn:
    """A plain text answer."""
    return ScriptedTurn(
        events=[*_text_events(text), BetaRawMessageStopEvent(type="message_stop")],
        message=_message(
            [BetaTextBlock(type="text", text=text)], stop_reason, container_id=container_id
        ),
        delay_s=delay_s,
    )


def turn_tool_use(
    calls: list[tuple[str, dict[str, Any]]],
    stop_reason: str = "tool_use",
    *,
    text: str | None = None,
    delay_s: float = 0.0,
    teardown_s: float = 0.0,
    container_id: str | None = None,
) -> ScriptedTurn:
    """One or more ``tool_use`` blocks, optionally preceded by some text."""
    events: list[Any] = []
    content: list[Any] = []
    index = 0
    if text is not None:
        events.extend(_text_events(text, index))
        content.append(BetaTextBlock(type="text", text=text))
        index += 1

    for position, (name, tool_input) in enumerate(calls):
        block = BetaToolUseBlock(
            type="tool_use", id=f"toolu_{position}", name=name, input=tool_input
        )
        events.append(
            BetaRawContentBlockStartEvent(
                type="content_block_start",
                index=index,
                content_block=BetaToolUseBlock(type="tool_use", id=block.id, name=name, input={}),
            )
        )
        for fragment in _chunks(json.dumps(tool_input), 6):
            events.append(
                BetaRawContentBlockDeltaEvent(
                    type="content_block_delta",
                    index=index,
                    delta=BetaInputJSONDelta(type="input_json_delta", partial_json=fragment),
                )
            )
        events.append(BetaRawContentBlockStopEvent(type="content_block_stop", index=index))
        content.append(block)
        index += 1

    events.append(BetaRawMessageStopEvent(type="message_stop"))
    return ScriptedTurn(
        events=events,
        message=_message(content, stop_reason, container_id=container_id),
        delay_s=delay_s,
        teardown_s=teardown_s,
    )


def turn_thinking_then_text(
    thinking: str, text: str, *, signature: str = "sig-abc123"
) -> ScriptedTurn:
    """A thinking block (with signature) followed by text."""
    block = BetaThinkingBlock(type="thinking", thinking=thinking, signature=signature)
    events: list[Any] = [
        BetaRawContentBlockStartEvent(
            type="content_block_start",
            index=0,
            content_block=BetaThinkingBlock(type="thinking", thinking="", signature=""),
        ),
        BetaRawContentBlockDeltaEvent(
            type="content_block_delta",
            index=0,
            delta=BetaThinkingDelta(type="thinking_delta", thinking=thinking),
        ),
        BetaRawContentBlockStopEvent(type="content_block_stop", index=0),
        *_text_events(text, 1),
        BetaRawMessageStopEvent(type="message_stop"),
    ]
    return ScriptedTurn(
        events=events,
        message=_message([block, BetaTextBlock(type="text", text=text)], "end_turn"),
    )


def turn_thinking_then_tool_use(
    thinking: str, calls: list[tuple[str, dict[str, Any]]], *, signature: str = "sig-abc123"
) -> ScriptedTurn:
    """A thinking block followed by tool calls — the replay-fidelity case."""
    block = BetaThinkingBlock(type="thinking", thinking=thinking, signature=signature)
    tool_turn = turn_tool_use(calls)
    return ScriptedTurn(
        events=[
            BetaRawContentBlockStartEvent(
                type="content_block_start",
                index=0,
                content_block=BetaThinkingBlock(type="thinking", thinking="", signature=""),
            ),
            BetaRawContentBlockDeltaEvent(
                type="content_block_delta",
                index=0,
                delta=BetaThinkingDelta(type="thinking_delta", thinking=thinking),
            ),
            BetaRawContentBlockStopEvent(type="content_block_stop", index=0),
            *tool_turn.events,
        ],
        message=_message([block, *tool_turn.message.content], "tool_use"),
    )


def turn_refusal(category: str | None = "cyber", explanation: str = "declined") -> ScriptedTurn:
    """A refusal: HTTP 200, ``stop_reason='refusal'``, and an EMPTY content list.

    The empty list is deliberate — any code that indexes ``content[0]`` without
    checking ``stop_reason`` first blows up on this fixture.
    """
    from anthropic.types.beta import BetaRefusalStopDetails

    return ScriptedTurn(
        events=[BetaRawMessageStopEvent(type="message_stop")],
        message=_message(
            [],
            "refusal",
            BetaRefusalStopDetails(type="refusal", category=category, explanation=explanation),
        ),
    )


def turn_pause(text: str = "searching") -> ScriptedTurn:
    """A ``pause_turn``: the server hit its own tool-iteration cap."""
    return ScriptedTurn(
        events=[*_text_events(text), BetaRawMessageStopEvent(type="message_stop")],
        message=_message([BetaTextBlock(type="text", text=text)], "pause_turn"),
    )


class _ScriptedStream:
    def __init__(self, turn: ScriptedTurn) -> None:
        self._turn = turn

    async def __aenter__(self) -> _ScriptedStream:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        if self._turn.teardown_s:
            await asyncio.sleep(self._turn.teardown_s)
        return False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for event in self._turn.events:
            if self._turn.delay_s:
                await asyncio.sleep(self._turn.delay_s)
            else:
                await asyncio.sleep(0)
            yield event

    async def get_final_message(self) -> BetaMessage:
        return self._turn.message


class _ScriptedMessages:
    def __init__(self, turns: list[ScriptedTurn], calls: list[dict[str, Any]]) -> None:
        self._turns = list(turns)
        self.calls = calls

    def stream(self, **kwargs: Any) -> _ScriptedStream:
        self.calls.append(kwargs)
        if not self._turns:
            raise AssertionError(f"ScriptedAnthropic ran out of turns on call {len(self.calls)}")
        return _ScriptedStream(self._turns.pop(0))


class _ScriptedBeta:
    def __init__(self, messages: _ScriptedMessages) -> None:
        self.messages = messages


class ScriptedAnthropic:
    """One scripted turn is consumed per ``beta.messages.stream()`` call.

    Every call's kwargs are recorded on ``.calls`` so tests can assert on
    ``betas``, ``fallbacks``, ``tools`` ordering, ``thinking``, ``output_config``
    and the exact ``messages`` array sent.
    """

    def __init__(self, turns: list[ScriptedTurn] | None = None, *, error: Exception | None = None):
        self.calls: list[dict[str, Any]] = []
        self._error = error
        self.beta = _ScriptedBeta(_ScriptedMessages(turns or [], self.calls))
        if error is not None:
            self.beta.messages.stream = self._raise  # type: ignore[method-assign]

    def _raise(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        raise self._error  # type: ignore[misc]

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def close(self) -> None:
        return None


def turn_code_execution(
    *,
    tool_use_id: str = "srvtoolu_0",
    command: str = "date -u",
    stdout: str = "Mon Sep 14 08:40:53 UTC 2026\n",
    return_code: int = 0,
    stop_reason: str = "end_turn",
    container_id: str | None = "cont_1",
    extra_content: list[Any] | None = None,
) -> ScriptedTurn:
    """A server-side code-execution call and its result, in one turn.

    This is what a `web_search_20260209` turn actually looks like: the dynamic
    filtering variant runs code execution under the hood, so blocks the app never
    declared a tool for show up in `content`.
    """
    use = BetaServerToolUseBlock(
        type="server_tool_use",
        id=tool_use_id,
        name="bash_code_execution",
        input={"command": command},
    )
    result = BetaBashCodeExecutionToolResultBlock(
        type="bash_code_execution_tool_result",
        tool_use_id=tool_use_id,
        content=BetaBashCodeExecutionResultBlock(
            type="bash_code_execution_result",
            content=[],
            return_code=return_code,
            stderr="",
            stdout=stdout,
        ),
    )
    content: list[Any] = [use, result, *(extra_content or [])]
    events: list[Any] = [
        BetaRawContentBlockStartEvent(type="content_block_start", index=0, content_block=use),
        BetaRawContentBlockStopEvent(type="content_block_stop", index=0),
        BetaRawContentBlockStartEvent(type="content_block_start", index=1, content_block=result),
        BetaRawContentBlockStopEvent(type="content_block_stop", index=1),
        BetaRawMessageStopEvent(type="message_stop"),
    ]
    return ScriptedTurn(
        events=events,
        message=_message(content, stop_reason, container_id=container_id),
    )


def turn_text_with_usage(
    text: str,
    *,
    input_tokens: int = 11,
    output_tokens: int = 7,
    cache_read: int = 0,
    cache_write: int = 0,
) -> ScriptedTurn:
    """A text turn whose usage counters are set explicitly."""
    turn = turn_text(text)
    turn.message.usage = BetaUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )
    return turn
