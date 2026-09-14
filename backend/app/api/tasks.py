"""A process-wide registry of in-flight streaming tasks, so they can be cancelled.

Keys are namespaced strings — ``"session:{id}"`` for a chat turn, and Task 6 will
register ``"note:{generation_id}"`` for a notes generation — so one registry and
one cancel endpoint pattern serve both.

Why this exists at all: **an SSE disconnect does not stop billing.** When the
browser goes away, the LLM call keeps running server-side unless something
actually cancels the task. The streaming endpoint therefore runs the consumption
in a task, registers it here, and both the cancel endpoint and the
disconnect-watcher call ``cancel``.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_tasks: dict[str, asyncio.Task] = {}
_lock = asyncio.Lock()

#: How long :func:`cancel_and_wait` waits for a task to actually stop. A task can
#: refuse to die — a shielded write, a handler that swallows CancelledError — and
#: an unbounded wait would hang the DELETE request behind it forever.
CANCEL_WAIT_S = 10.0


def session_key(session_id: int) -> str:
    return f"session:{session_id}"


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
    done, _pending = await asyncio.wait({task}, timeout=CANCEL_WAIT_S)
    if not done:
        # Proceed anyway: the caller's work matters more than a wedged task, and
        # anything it still manages to write now fails inside run()'s safety net
        # rather than escaping.
        logger.warning(
            "Task %s did not stop within %.0fs of being cancelled; continuing without it",
            key,
            CANCEL_WAIT_S,
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
    "CANCEL_WAIT_S",
    "cancel",
    "cancel_and_wait",
    "clear",
    "is_running",
    "register",
    "session_key",
    "unregister",
]
