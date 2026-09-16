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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import events as ev
from app.agent.turnlog import TurnLog
from app.api.tasks import CANCEL_WAIT_S
from app.db.models import ResearchSession, utcnow

logger = logging.getLogger(__name__)

CANCELLED_MESSAGE = "The turn was stopped before it finished."
CRASHED_MESSAGE = "The turn failed unexpectedly; see the server log."


class TurnAlreadyRunning(Exception):
    """A session has one turn at a time."""


@dataclass
class RunningTurn:
    turn_id: str
    session_id: int
    started_at: datetime
    log: TurnLog
    task: asyncio.Task = field(repr=False)


async def _set_status(
    session_factory: async_sessionmaker[AsyncSession],
    session_id: int,
    status: str,
    started_at: datetime | None,
) -> None:
    async with session_factory() as session:
        await session.execute(
            update(ResearchSession)
            .where(ResearchSession.id == session_id)
            .values(turn_status=status, turn_started_at=started_at)
        )
        await session.commit()


class TurnRegistry:
    def __init__(self) -> None:
        self._turns: dict[int, RunningTurn] = {}

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
        """Own *generator* as this session's turn. Raises ``TurnAlreadyRunning``."""
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
        # never sees a running turn with an idle row.
        await _set_status(session_factory, session_id, "running", started_at)

        task = asyncio.create_task(
            self._drive(session_id, session_factory, generator, client, log),
            name=f"turn:{session_id}",
        )
        turn = RunningTurn(turn_id, session_id, started_at, log, task)
        self._turns[session_id] = turn
        return turn

    async def _drive(
        self,
        session_id: int,
        session_factory: async_sessionmaker[AsyncSession],
        generator: AsyncIterator[ev.AgentEvent],
        client: Any | None,
        log: TurnLog,
    ) -> None:
        saw_done = False
        terminal: ev.Error | None = None
        try:
            async for event in generator:
                log.append(event)
                saw_done = saw_done or isinstance(event, ev.Done)
        except asyncio.CancelledError:
            terminal = ev.Error(error_type="cancelled", message=CANCELLED_MESSAGE)
            # Swallowed on purpose: the turn's own cleanup below must run, and the
            # cancellation has done its job (the runner has already persisted
            # what it could on its way out).
        except Exception:  # noqa: BLE001 - the log must always end
            logger.exception("Turn for session %s crashed", session_id)
            terminal = ev.Error(error_type="api_error", message=CRASHED_MESSAGE)
        finally:
            # Shielded: a cancel that lands during this cleanup must not leave a
            # half-closed log or a row stuck on ``running``.
            await asyncio.shield(
                self._finish(session_id, session_factory, client, log, terminal, saw_done)
            )

    async def _finish(
        self,
        session_id: int,
        session_factory: async_sessionmaker[AsyncSession],
        client: Any | None,
        log: TurnLog,
        terminal: ev.Error | None,
        saw_done: bool,
    ) -> None:
        try:
            if terminal is not None:
                log.append(terminal)
            if terminal is not None or not saw_done:
                log.append(ev.Done(session_id=session_id))
            log.close()
            if client is not None:
                try:
                    await client.close()
                except Exception:  # noqa: BLE001 - never let a close hide the turn's end
                    logger.warning("Closing the turn's client failed", exc_info=True)
            try:
                await _set_status(session_factory, session_id, "idle", None)
            except Exception:  # noqa: BLE001 - the app may be shutting down
                logger.warning(
                    "Could not idle session %s after its turn", session_id, exc_info=True
                )
        finally:
            self._turns.pop(session_id, None)

    async def cancel(self, session_id: int) -> bool:
        turn = self.get(session_id)
        if turn is None:
            return False
        turn.task.cancel()
        logger.info("Cancelled the turn for session %s", session_id)
        return True

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

    async def drain(self) -> None:
        """Cancel every turn and wait (bounded) — shutdown."""
        turns = [turn for turn in self._turns.values() if not turn.task.done()]
        for turn in turns:
            turn.task.cancel()
        if turns:
            await asyncio.wait({turn.task for turn in turns}, timeout=CANCEL_WAIT_S)


__all__ = ["CANCELLED_MESSAGE", "RunningTurn", "TurnAlreadyRunning", "TurnRegistry"]
