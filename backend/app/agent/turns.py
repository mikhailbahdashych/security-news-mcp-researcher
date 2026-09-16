"""Running chat turns, owned by the process rather than by a request.

A turn used to live inside the SSE response that started it, and a disconnect
watcher cancelled it a second after the browser went away. Now the turn is a
task held here, keyed by session; the request that started it returns at once,
and any number of ``GET /stream`` subscribers can attach to its :class:`TurnLog`
and leave again without touching the run. Only ``cancel`` stops a turn.

The runner is untouched: it still persists message by message and still writes
the interrupted tool results on ``CancelledError``. What this module adds is the
owner — the task, the client's lifetime, the log, and the session row's
``turn_status``.

Three rules hold the invariants together:

* **One turn per session**, enforced by a lock that covers the check, the row
  write and the registration — not by the check alone.
* **A turn is forgotten before anyone can see it end.** ``done`` reaching a
  subscriber means the next message may arrive in the very next request, so the
  registry entry goes first and the bookkeeping follows.
* **The cleanup always runs to the end.** It is shielded *and* re-shielded, so a
  second cancel (Stop, then Delete; Stop, then shutdown) cannot detach it and
  leave a client open or a row stuck on ``running``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import events as ev
from app.agent.turnlog import TurnLog
from app.db.models import ResearchSession, utcnow

logger = logging.getLogger(__name__)

#: How long the bounded waits here — :meth:`TurnRegistry.cancel_and_wait` and
#: :meth:`TurnRegistry.drain` — give a cancelled turn to actually stop. A task can
#: refuse to die (a shielded write, a handler that swallows ``CancelledError``),
#: and an unbounded wait would hang the DELETE or the shutdown behind it forever.
#: ``app.api.tasks`` imports it from here: the agent layer must not import the API
#: layer, and the two waits are the same promise.
CANCEL_WAIT_S = 10.0

CANCELLED_MESSAGE = "The turn was stopped before it finished."
CRASHED_MESSAGE = "The turn failed unexpectedly; see the server log."


class TurnAlreadyRunning(Exception):
    """A session has one turn at a time."""


class TurnStartFailed(Exception):
    """The turn could not be marked running, so it was never started."""


@dataclass
class RunningTurn:
    turn_id: str
    session_id: int
    started_at: datetime
    log: TurnLog
    session_factory: async_sessionmaker[AsyncSession] = field(repr=False)
    client: Any | None = field(default=None, repr=False)
    #: Set — synchronously, before any await — by the first call to ``_finish``.
    #: Every path that can close a turn out (the task's own cleanup, a cancel that
    #: landed before the task ever ran) goes through that one latch, so none of
    #: them can append a second terminal pair or close the log twice.
    finished: bool = field(default=False, repr=False)
    task: asyncio.Task = field(init=False, repr=False)


async def _set_status(
    session_factory: async_sessionmaker[AsyncSession],
    session_id: int,
    status: str,
    started_at: datetime | None,
    *,
    only_if_started_at: datetime | None = None,
) -> None:
    """Write one session's turn state.

    ``only_if_started_at`` makes the write a compare-and-set: a turn that has
    already been replaced by a newer one must not flip its successor's row.
    """
    statement = update(ResearchSession).where(ResearchSession.id == session_id)
    if only_if_started_at is not None:
        statement = statement.where(ResearchSession.turn_started_at == only_if_started_at)
    async with session_factory() as session:
        await session.execute(
            statement.values(
                turn_status=status,
                turn_started_at=started_at,
                # The sidebar is ordered by ``updated_at`` and the column carries an
                # ``onupdate``. Assigning it to itself keeps a turn's own bookkeeping
                # from reordering the user's history twice per question.
                updated_at=ResearchSession.updated_at,
            )
        )
        await session.commit()


class TurnRegistry:
    def __init__(self) -> None:
        self._turns: dict[int, RunningTurn] = {}
        #: Covers the whole of ``start`` — the running check, the row write and the
        #: registration. Checking without it let two concurrent POSTs both pass and
        #: start two turns on one transcript, the first of them unreachable.
        self._start_lock = asyncio.Lock()
        #: The in-flight ``_finish`` calls, so a turn that is already past ``done``
        #: is still waited for at shutdown — and so the loop keeps a strong
        #: reference to every one of them.
        self._finishing: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ reads

    def get(self, session_id: int) -> RunningTurn | None:
        turn = self._turns.get(session_id)
        return turn if turn is not None and not turn.task.done() else None

    def is_running(self, session_id: int) -> bool:
        return self.get(session_id) is not None

    def running_ids(self) -> list[int]:
        return [sid for sid, turn in self._turns.items() if not turn.task.done()]

    # ----------------------------------------------------------------- writes

    async def start(
        self,
        *,
        session_id: int,
        session_factory: async_sessionmaker[AsyncSession],
        generator: AsyncIterator[ev.AgentEvent],
        client: Any | None,
        prompt: str,
        attachments: list[dict[str, Any]],
    ) -> RunningTurn:
        """Own *generator* as this session's turn.

        Raises ``TurnAlreadyRunning`` if one is in flight, and ``TurnStartFailed``
        if the session row could not be marked running — in which case no task was
        created and the caller owns the client it passed.
        """
        async with self._start_lock:
            if self.is_running(session_id):
                raise TurnAlreadyRunning(session_id)

            started_at = utcnow()
            log = TurnLog()
            turn_id = uuid.uuid4().hex
            log.append(
                ev.TurnStarted(
                    turn_id=turn_id,
                    session_id=session_id,
                    prompt=prompt,
                    attachments=attachments,
                    started_at=started_at,
                )
            )
            # Written before the task exists so a list request racing the start
            # never sees a running turn with an idle row. Guarded, because a
            # failure here has to be the caller's error rather than a task that
            # was never created and a row that never said so.
            try:
                await _set_status(session_factory, session_id, "running", started_at)
            except SQLAlchemyError as exc:
                logger.exception("Could not mark session %s running; turn not started", session_id)
                raise TurnStartFailed(session_id) from exc

            turn = RunningTurn(
                turn_id=turn_id,
                session_id=session_id,
                started_at=started_at,
                log=log,
                session_factory=session_factory,
                client=client,
            )
            turn.task = asyncio.create_task(self._drive(turn, generator), name=f"turn:{session_id}")
            self._turns[session_id] = turn
            return turn

    def _forget(self, turn: RunningTurn) -> None:
        """Drop *turn*'s registration — by identity, never by key alone.

        Popping by key would let a turn that outlived its registration (one that
        refused to stop, one started by a racing request) evict the turn actually
        running now.
        """
        if self._turns.get(turn.session_id) is turn:
            del self._turns[turn.session_id]

    async def _drive(self, turn: RunningTurn, generator: AsyncIterator[ev.AgentEvent]) -> None:
        saw_done = False
        terminal: ev.Error | None = None
        try:
            async for event in generator:
                if isinstance(event, ev.Done):
                    saw_done = True
                    # Before the event reaches a subscriber: a client that has seen
                    # ``done`` may send its next message immediately, and a turn
                    # still in the registry would answer that with a 409.
                    self._forget(turn)
                turn.log.append(event)
        except asyncio.CancelledError:
            terminal = ev.Error(error_type="cancelled", message=CANCELLED_MESSAGE)
            # Swallowed on purpose: the turn's own cleanup below must run, and the
            # cancellation has done its job (the runner has already persisted
            # what it could on its way out).
        except Exception:  # noqa: BLE001 - the log must always end
            logger.exception("Turn for session %s crashed", turn.session_id)
            terminal = ev.Error(error_type="api_error", message=CRASHED_MESSAGE)
        finally:
            self._forget(turn)
            # Shielded *in a loop*: a shield only protects the cleanup, it does not
            # keep this task attached to it. A second cancel — Stop then Delete,
            # Stop then shutdown — would otherwise finish the task while the
            # cleanup ran on as an orphan, leaving the client open and the row on
            # ``running`` after ``drain()`` had already returned.
            cleanup = asyncio.ensure_future(self._finish(turn, terminal, saw_done))
            self._finishing.add(cleanup)
            cleanup.add_done_callback(self._finishing.discard)
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue

    async def _finish(self, turn: RunningTurn, terminal: ev.Error | None, saw_done: bool) -> None:
        """End the log, close the client, idle the row. Runs at most once."""
        if turn.finished:
            return
        turn.finished = True
        self._forget(turn)
        if not turn.log.closed:
            if not saw_done:
                # A turn that already said ``done`` was not stopped in any way the
                # user should see: a cancel landing after it is bookkeeping, not a
                # second ending.
                if terminal is not None:
                    turn.log.append(terminal)
                turn.log.append(ev.Done(session_id=turn.session_id))
            turn.log.close()
        if turn.client is not None:
            try:
                await turn.client.close()
            except Exception:  # noqa: BLE001 - never let a close hide the turn's end
                logger.warning("Closing the turn's client failed", exc_info=True)
        try:
            await _set_status(
                turn.session_factory,
                turn.session_id,
                "idle",
                None,
                only_if_started_at=turn.started_at,
            )
        except Exception:  # noqa: BLE001 - the app may be shutting down
            logger.warning(
                "Could not idle session %s after its turn", turn.session_id, exc_info=True
            )

    async def _finalise_cancelled(self, turn: RunningTurn) -> None:
        """Close out a turn whose task never ran.

        ``create_task`` does not start the coroutine until the next loop pass, so a
        cancel before that pass means ``_drive`` — and its cleanup — never execute.
        Nothing would then close the log or idle the row. ``_finish`` is latched,
        so calling it here is a no-op whenever the task did run.
        """
        await self._finish(
            turn, ev.Error(error_type="cancelled", message=CANCELLED_MESSAGE), saw_done=False
        )

    async def cancel(self, session_id: int) -> bool:
        turn = self.get(session_id)
        if turn is None:
            return False
        turn.task.cancel()
        # Follow the task to its end, in case it is cancelled before it ever ran
        # (see ``_finalise_cancelled``). Tracked so the loop keeps a reference.
        follow = asyncio.ensure_future(self._follow_and_finalise(turn))
        self._finishing.add(follow)
        follow.add_done_callback(self._finishing.discard)
        logger.info("Cancelled the turn for session %s", session_id)
        return True

    async def _follow_and_finalise(self, turn: RunningTurn) -> None:
        await asyncio.gather(turn.task, return_exceptions=True)
        await self._finalise_cancelled(turn)

    async def cancel_and_wait(self, session_id: int) -> bool:
        turn = self.get(session_id)
        if turn is None:
            return False
        turn.task.cancel()
        done, _ = await asyncio.wait({turn.task}, timeout=CANCEL_WAIT_S)
        if not done:
            logger.warning(
                "Turn for session %s did not stop within %.0fs; continuing without it",
                session_id,
                CANCEL_WAIT_S,
            )
            return True
        await self._finalise_cancelled(turn)
        return True

    async def drain(self) -> None:
        """Cancel every turn and wait (bounded) — shutdown."""
        turns = [turn for turn in self._turns.values() if not turn.task.done()]
        for turn in turns:
            turn.task.cancel()
        if turns:
            await asyncio.wait({turn.task for turn in turns}, timeout=CANCEL_WAIT_S)
            for turn in turns:
                if turn.task.done():
                    await self._finalise_cancelled(turn)
        # Turns already past ``done`` are no longer registered but may still be
        # writing their last row; the engine goes away right after this.
        if self._finishing:
            await asyncio.wait(set(self._finishing), timeout=CANCEL_WAIT_S)


async def mark_interrupted(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Startup: a row still ``running`` belongs to a process that died mid-turn."""
    async with session_factory() as session:
        result = await session.execute(
            update(ResearchSession)
            .where(ResearchSession.turn_status == "running")
            .values(turn_status="interrupted", updated_at=ResearchSession.updated_at)
        )
        await session.commit()
        return int(result.rowcount or 0)


__all__ = [
    "CANCELLED_MESSAGE",
    "CANCEL_WAIT_S",
    "RunningTurn",
    "TurnAlreadyRunning",
    "TurnRegistry",
    "TurnStartFailed",
    "mark_interrupted",
]
