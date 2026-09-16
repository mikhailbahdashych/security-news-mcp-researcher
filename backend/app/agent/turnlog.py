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
        #: Flipped by every append and by ``close``; a waiter clears it and
        #: re-checks. An ``asyncio.Event`` may be built outside a running loop, so
        #: a log can be created and closed anywhere.
        self._changed = asyncio.Event()

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
        # Synchronous, and free: every waiter parked in ``subscribe`` is resumed on
        # the next loop pass. A turn is thousands of events long, so waking them
        # through a scheduled task each time cost a task — and hundreds of
        # milliseconds of loop time — per turn for nothing.
        self._changed.set()

    async def subscribe(self) -> AsyncIterator[ev.AgentEvent]:
        """Every event so far, then each new one, until the log is closed."""
        index = 0
        while True:
            while index < len(self._events):
                yield self._events[index]
                index += 1
            if self._closed:
                return
            # Clear first, then re-check, then wait — with no await in between, so
            # an append can only land before the check (seen now) or after the
            # clear (which sets the flag again and returns the wait immediately).
            self._changed.clear()
            if index < len(self._events) or self._closed:
                continue
            await self._changed.wait()


__all__ = ["TurnLog"]
