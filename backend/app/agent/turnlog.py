"""The replayable record of one running turn.

A turn's page can go away and come back — a reload, a trip to the Inbox, a
second tab — and it has to see the turn *from the start*, not from wherever the
stream happens to be. So the turn's events are kept, in order, for as long as
the turn runs, and a subscriber replays them and then tails.

The log is dropped with the turn: once ``done`` is in the database the
transcript is the record, and this is only ever the in-flight view.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from app.agent import events as ev


class TurnLog:
    def __init__(self) -> None:
        self._events: list[ev.AgentEvent] = []
        self._closed = False
        self._changed = asyncio.Condition()
        self._wakeups: set[asyncio.Task[None]] = set()

    @property
    def events(self) -> list[ev.AgentEvent]:
        return list(self._events)

    @property
    def closed(self) -> bool:
        return self._closed

    def append(self, event: ev.AgentEvent) -> None:
        if self._closed:
            raise RuntimeError("turn log is closed")
        self._events.append(event)
        self._notify()

    def close(self) -> None:
        self._closed = True
        self._notify()

    def _notify(self) -> None:
        # Called from the owning task, on the loop; the condition's lock is only
        # ever contended by waiters, so this never blocks the writer.
        async def wake() -> None:
            async with self._changed:
                self._changed.notify_all()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop, so no subscribers: a log built and closed outside one has
            # nothing to wake.
            return
        # The loop only holds a weak reference to a task, so an unreferenced wake
        # can be collected before it runs — and a waiter would then sleep until
        # the next append instead of seeing this one.
        task = loop.create_task(wake())
        self._wakeups.add(task)
        task.add_done_callback(self._wakeups.discard)

    async def subscribe(self) -> AsyncIterator[ev.AgentEvent]:
        """Every event so far, then each new one, until the log is closed."""
        index = 0
        while True:
            while index < len(self._events):
                yield self._events[index]
                index += 1
            if self._closed:
                return
            async with self._changed:
                # Re-check under the lock: a notify that landed between the
                # length check and the wait would otherwise be missed.
                if index < len(self._events) or self._closed:
                    continue
                await self._changed.wait()


__all__ = ["TurnLog"]
