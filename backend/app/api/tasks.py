"""A process-wide registry of in-flight streaming tasks, so they can be cancelled.

Keys are namespaced strings; ``app/api/notes.py::generation_key`` builds the only
one left, ``"note:{generation_id}"``. A chat turn used to be registered here as
``"session:{id}"`` and is not any more: it belongs to
:class:`app.agent.turns.TurnRegistry`, which owns the task for the whole of its
life rather than for the length of one request.

Why this exists at all: **an SSE disconnect does not stop billing.** When the
browser goes away, the LLM call keeps running server-side unless something
actually cancels the task. The streaming endpoint therefore runs the consumption
in a task, registers it here, and both the cancel endpoint and the
disconnect-watcher call ``cancel``.
"""

from __future__ import annotations

import asyncio
import logging

# The bound lives with the turn registry, which makes the same promise about the
# same kind of task. The *module* is imported, not the value: ``from ... import
# CANCEL_WAIT_S`` copies the float at import time, so the two halves of the app
# would drift apart the moment anything (a test, a setting) changed it. And in
# this direction only: domain code under app/agent must never import the API layer.
from app.agent import turns

logger = logging.getLogger(__name__)

_tasks: dict[str, asyncio.Task] = {}
_lock = asyncio.Lock()


async def register(key: str, task: asyncio.Task) -> None:
    """Claim *key* for *task*. Raises ``KeyError`` if a turn is already running."""
    async with _lock:
        existing = _tasks.get(key)
        if existing is not None and not existing.done():
            raise KeyError(key)
        _tasks[key] = task


async def is_running(key: str) -> bool:
    async with _lock:
        task = _tasks.get(key)
        return task is not None and not task.done()


async def cancel(key: str) -> bool:
    """Cancel the task under *key*. ``False`` when nothing is running."""
    async with _lock:
        task = _tasks.get(key)
    if task is None or task.done():
        return False
    task.cancel()
    logger.info("Cancelled in-flight task %s", key)
    return True


async def cancel_and_wait(key: str) -> bool:
    """Cancel the task under *key* and wait for it to actually stop.

    ``cancel`` only *requests* cancellation; the task keeps running until the next
    await point, and it may be mid-commit. A caller that is about to delete rows
    the task writes to has to wait, or the task races it and writes a row for a
    session that no longer exists.
    """
    async with _lock:
        task = _tasks.get(key)
    if task is None or task.done():
        return False
    task.cancel()
    # asyncio.wait never re-raises the task's exception and never cancels the
    # caller, so a task that dies of anything (including the CancelledError we
    # just caused) is simply reported as done.
    done, _pending = await asyncio.wait({task}, timeout=turns.CANCEL_WAIT_S)
    if not done:
        # Proceed anyway: the caller's work matters more than a wedged task, and
        # anything it still manages to write now fails inside run()'s safety net
        # rather than escaping.
        logger.warning(
            "Task %s did not stop within %.0fs of being cancelled; continuing without it",
            key,
            turns.CANCEL_WAIT_S,
        )
    else:
        logger.info("Cancelled and awaited in-flight task %s", key)
    return True


async def unregister(key: str) -> None:
    async with _lock:
        _tasks.pop(key, None)


async def clear() -> None:
    """Drop every registration — for tests, and for a clean shutdown."""
    async with _lock:
        _tasks.clear()


__all__ = [
    "cancel",
    "cancel_and_wait",
    "clear",
    "is_running",
    "register",
    "unregister",
]
