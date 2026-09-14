"""The SSE plumbing shared by the chat turn and note generation.

Both routes stream the *same* runner over the *same* transport, and both have to
get the same three things right:

* **The run must outlive the client.** An SSE disconnect does not stop billing —
  the LLM call keeps running server-side — so the runner is consumed inside a
  task registered in :mod:`app.api.tasks`, and a disconnect cancels that task
  rather than merely stopping the writes.
* **No buffering.** ``ping=15`` keeps the connection alive through a long
  thinking pause and ``X-Accel-Buffering: no`` stops a proxy holding it back.
* **One run per key.** A second POST while one is running is a conflict, not a
  second billed turn.

What is deliberately *not* here is the terminal event. Chat ends on the runner's
own ``done``; note generation replaces it with its own ``{"note_id": ...}`` and
emits nothing at all when the generation failed. That difference belongs to the
route, so this module yields the runner's events and lets the caller encode them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from anthropic import AsyncAnthropic
from fastapi import Request

from app.agent import events as ev
from app.api import tasks as task_registry

logger = logging.getLogger(__name__)

#: Heartbeat interval for every streamed route.
SSE_PING_S = 15

#: Response headers every streamed route sets. No gzip anywhere in the app, so
#: there is nothing here to switch it off with.
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

#: How long the disconnect watcher waits between polls of ``is_disconnected``.
DISCONNECT_POLL_S = 1.0

CANCELLED_MESSAGE = "The turn was stopped before it finished."
ALREADY_RUNNING_MESSAGE = "A turn is already running."


def sse_data(event: str, payload: dict[str, Any]) -> dict[str, str]:
    """One SSE frame, as ``EventSourceResponse`` wants it."""
    return {"event": event, "data": json.dumps(payload, default=str)}


def sse_frame(event: ev.AgentEvent) -> dict[str, str]:
    """Encode one agent event onto the wire."""
    return sse_data(*event.to_sse())


async def frames(*events: ev.AgentEvent) -> AsyncIterator[dict[str, str]]:
    """A canned stream, for a failure visible before the runner ever starts."""
    for event in events:
        yield sse_frame(event)


async def pump_agent_events(
    request: Request,
    generator: AsyncIterator[ev.AgentEvent],
    *,
    key: str,
    client: AsyncAnthropic | None = None,
) -> AsyncIterator[ev.AgentEvent]:
    """Consume *generator* inside a cancellable task, yielding its events.

    The runner is driven by a task registered under *key* so that a client
    disconnect or a POST to a cancel endpoint can cancel the LLM call itself.
    Whatever the runner already committed stays committed; only the un-run
    remainder is dropped.

    Two events are produced by this function rather than by the runner:

    * ``api_error`` when *key* is already claimed — the caller is a duplicate and
      must not start a second billed turn;
    * ``cancelled`` as the last event when the run was stopped early.

    *client* is closed when the stream finalises: a streaming route owns its
    client's lifetime (see ``app.api.deps.get_chat_client_factory``).
    """
    queue: asyncio.Queue[ev.AgentEvent | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for event in generator:
                await queue.put(event)
        finally:
            await queue.put(None)

    task = asyncio.create_task(pump())
    try:
        await task_registry.register(key, task)
    except KeyError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if client is not None:
            await client.close()
        yield ev.Error(error_type="api_error", message=ALREADY_RUNNING_MESSAGE)
        return

    cancelled = False
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=DISCONNECT_POLL_S)
            except TimeoutError:
                # SSE disconnect alone does not stop billing, so a gone client
                # must actually cancel the in-flight stream.
                if await request.is_disconnected():
                    task.cancel()
                    cancelled = True
                    break
                continue

            if event is None:
                break
            yield event
    except asyncio.CancelledError:
        task.cancel()
        raise
    finally:
        if not task.done():
            task.cancel()
            cancelled = True
        await asyncio.gather(task, return_exceptions=True)
        # A POST to a cancel endpoint kills the pump task directly, which still
        # drains the queue cleanly — so the cancellation shows up here, not in
        # the loop.
        cancelled = cancelled or task.cancelled()
        await task_registry.unregister(key)
        if client is not None:
            await client.close()
        if cancelled:
            logger.info("Stream %s ended early", key)

    if cancelled:
        yield ev.Error(error_type="cancelled", message=CANCELLED_MESSAGE)


__all__ = [
    "ALREADY_RUNNING_MESSAGE",
    "CANCELLED_MESSAGE",
    "DISCONNECT_POLL_S",
    "SSE_HEADERS",
    "SSE_PING_S",
    "frames",
    "pump_agent_events",
    "sse_data",
    "sse_frame",
]
