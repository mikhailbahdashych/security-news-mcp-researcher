# Background Research Turns Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A research turn keeps running after the page that started it reloads or navigates away, and any page can attach to it and watch it live.

**Architecture:** The chat turn moves out of the HTTP request into a session-owned `asyncio.Task` held by a process-wide `TurnRegistry`. Each running turn writes its events into a replayable `TurnLog`; `POST /messages` starts the task and returns 202, and a new `GET /stream` replays the log then tails it over SSE. The runner and persistence are untouched; the frontend's live-turn reducer gains one event (`turn_started`) so a late attach renders like the original send.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2 async + aiosqlite / sse-starlette / pytest (scripted Anthropic fake) — React 19 / TypeScript / TanStack Query / react-router 7 / vitest.

**Spec:** `docs/superpowers/specs/2026-09-16-background-turns-design.md`

## Global Constraints

- No schedulers, no cron, no background polling. The only refetch trigger added is window focus plus the page's own events.
- No network in tests: Anthropic through `tests/fakes/anthropic.py` (`ScriptedAnthropic`, `turn_text`, `turn_tool_use` with `delay_s`), HTTP through `httpx2.ASGITransport`.
- `httpx2`, never `httpx`. All `DATETIME` columns are naive UTC via `app.db.models.utcnow()`.
- No Alembic: the two new columns mean the dev DB (`backend/data/app.db`) must be deleted; the PR body says so.
- Plain commit messages, explicit `git add` paths, no attribution trailers. Never merge; never push (the controller pushes).
- Frontend: no jsdom; logic that deserves a test lives in a `.ts` module. Tailwind tokens from `frontend/CLAUDE.md` (`bg-panel`, `text-faint`, `border-line`, …).
- Read `backend/CLAUDE.md`, `backend/app/agent/CLAUDE.md`, `frontend/CLAUDE.md` before the first task in each half.

---

## File map

Backend
- Create `backend/app/agent/turnlog.py` — `TurnLog`: append-only event list + condition; `subscribe()` replays then tails.
- Create `backend/app/agent/turns.py` — `TurnRegistry`, `RunningTurn`, `TurnAlreadyRunning`; owns the task, client and log per session; writes `turn_status`.
- Modify `backend/app/agent/events.py` — add `TurnStarted`.
- Modify `backend/app/db/models.py` — `ResearchSession.turn_status`, `turn_started_at`.
- Modify `backend/app/schemas/sessions.py` — `SessionRead.turn_status`, `turn_started_at`; new `TurnAccepted`, `RunningSessions`.
- Modify `backend/app/api/deps.py` — `TurnRegistryDep`.
- Modify `backend/app/api/streaming.py` — `stream_turn_log(request, log)`.
- Modify `backend/app/api/sessions.py` — POST → 202; `GET /sessions/running`; `GET /sessions/{id}/stream`; cancel/delete via the registry.
- Modify `backend/app/main.py` — registry on `app.state` in `create_app`; lifespan marks `running` → `interrupted` on start and drains on shutdown.
- Tests: `backend/tests/test_turnlog.py`, `backend/tests/test_turn_registry.py`, `backend/tests/test_api_sessions_stream.py`; existing `test_api_sessions.py` turn tests updated.

Frontend
- Modify `frontend/src/api/chat.ts` — session fields, `startTurn`, `streamUrl`, `fetchRunningSessions`, `TurnStartedPayload`, `resendPayload`.
- Modify `frontend/src/components/chat/liveTurn.ts` — `turn_started` case; `attachToken`.
- Modify `frontend/src/pages/ChatPage.tsx` — POST-then-attach send, attach on load, detach-only abandon, interrupted notice, header meta.
- Create `frontend/src/components/chat/InterruptedNotice.tsx`.
- Create `frontend/src/lib/useRunningTurns.ts`.
- Modify `frontend/src/components/ui/Rail.tsx` (activity dot), `frontend/src/components/chat/HistoryDrawer.tsx` (running mark).
- Tests: `liveTurn.test.ts`, `chat.test.ts`, new `lib/useRunningTurns.test.ts` (pure part only).

Docs: `backend/CLAUDE.md`, `backend/app/agent/CLAUDE.md`, `frontend/CLAUDE.md`, `docs/DESIGN.md`, root `CLAUDE.md`.

---

### Task 1: Turn state on the session and the `turn_started` event

**Files:**
- Modify: `backend/app/db/models.py:118-129` (`ResearchSession`)
- Modify: `backend/app/schemas/sessions.py:16-28` (`SessionRead`) and append two schemas
- Modify: `backend/app/agent/events.py` (add `TurnStarted`, extend the `AgentEvent` union)
- Test: `backend/tests/test_api_sessions.py` (append), `backend/tests/test_events.py` (create if absent)

**Interfaces:**
- Produces: `ResearchSession.turn_status: str` (`"idle" | "running" | "interrupted"`, default `"idle"`), `ResearchSession.turn_started_at: datetime | None`; `SessionRead.turn_status`, `SessionRead.turn_started_at`; `TurnAccepted(turn_id: str, session_id: int, started_at: datetime)`; `RunningSessions(session_ids: list[int])`; `ev.TurnStarted(turn_id, session_id, prompt, attachments, started_at)` with `type == "turn_started"`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_api_sessions.py`:

```python
async def test_a_new_session_reports_an_idle_turn(client):
    session_id = await create_session(client)
    body = (await client.get(f"/api/sessions/{session_id}")).json()["session"]
    assert body["turn_status"] == "idle"
    assert body["turn_started_at"] is None
```

Create `backend/tests/test_events.py`:

```python
from datetime import datetime

from app.agent import events as ev


def test_turn_started_serialises_the_attach_context():
    event = ev.TurnStarted(
        turn_id="t-1",
        session_id=7,
        prompt="what happened?",
        attachments=[{"id": 3, "title": "An item", "url": "https://example.test/a"}],
        started_at=datetime(2026, 9, 16, 10, 0, 0),
    )
    name, payload = event.to_sse()
    assert name == "turn_started"
    assert payload == {
        "turn_id": "t-1",
        "session_id": 7,
        "prompt": "what happened?",
        "attachments": [{"id": 3, "title": "An item", "url": "https://example.test/a"}],
        "started_at": "2026-09-16T10:00:00",
    }
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/test_api_sessions.py::test_a_new_session_reports_an_idle_turn tests/test_events.py -q`
Expected: FAIL — `KeyError: 'turn_status'` and `AttributeError: module 'app.agent.events' has no attribute 'TurnStarted'`.

- [ ] **Step 3: Add the columns, the schema fields and the event**

In `backend/app/db/models.py`, inside `ResearchSession` after `total_output_tokens`:

```python
    #: ``idle`` | ``running`` | ``interrupted``. ``running`` is written when a turn's
    #: task starts and ``idle`` when it ends however it ends; ``interrupted`` is
    #: written at startup for rows still ``running`` — the process died mid-turn.
    turn_status: Mapped[str] = mapped_column(Text, nullable=False, default="idle")
    turn_started_at: Mapped[datetime | None] = mapped_column(DateTime)
```

(`DateTime` is already imported in that module; check the import line.)

In `backend/app/schemas/sessions.py`, add to `SessionRead` after `updated_at`:

```python
    turn_status: Literal["idle", "running", "interrupted"] = "idle"
    turn_started_at: datetime | None = None
```

(add `Literal` to the `typing` import) and append at the end of the file:

```python
class TurnAccepted(BaseModel):
    """``POST /sessions/{id}/messages``: the turn is running; attach to ``/stream``."""

    turn_id: str
    session_id: int
    started_at: datetime


class RunningSessions(BaseModel):
    """Which sessions have a turn in flight right now, straight from the registry."""

    session_ids: list[int]
```

In `backend/app/agent/events.py`, before `class TurnStart`:

```python
@dataclass(frozen=True, slots=True)
class TurnStarted:
    """The first event in every turn log: what a late subscriber needs to render
    the question, the attachment chips and the elapsed counter.

    Distinct from ``TurnStart`` (one per *API* turn inside the loop); this is one
    per *user* turn and is produced by the turn registry, never by the runner.
    """

    turn_id: str
    session_id: int
    prompt: str
    attachments: list[dict[str, Any]]
    started_at: datetime
    type: str = "turn_started"

    def to_sse(self) -> SSEEvent:
        return self.type, {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "prompt": self.prompt,
            "attachments": self.attachments,
            "started_at": self.started_at.isoformat(),
        }
```

Add `from datetime import datetime` at the top and `TurnStarted` to the `AgentEvent` union and `__all__` if there is one.

- [ ] **Step 4: Run the tests**

Run: `cd backend && uv run pytest tests/test_api_sessions.py tests/test_events.py -q`
Expected: PASS (the whole sessions file still green).

- [ ] **Step 5: Commit**

```bash
git add backend/app/db/models.py backend/app/schemas/sessions.py backend/app/agent/events.py backend/tests/test_api_sessions.py backend/tests/test_events.py
git commit -m "Give a session a turn state and the turn its opening event"
```

---

### Task 2: `TurnLog` — replay, then tail

**Files:**
- Create: `backend/app/agent/turnlog.py`
- Test: `backend/tests/test_turnlog.py`

**Interfaces:**
- Produces: `TurnLog()` with `append(event: AgentEvent) -> None`, `close() -> None`, `closed: bool`, `events: list[AgentEvent]` (read-only view), and `subscribe() -> AsyncIterator[AgentEvent]` which yields every event from index 0 and then waits for new ones until `close()`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_turnlog.py`:

```python
import asyncio

from app.agent import events as ev
from app.agent.turnlog import TurnLog


async def _collect(log: TurnLog) -> list[str]:
    return [event.type async for event in log.subscribe()]


async def test_a_late_subscriber_replays_then_tails():
    log = TurnLog()
    log.append(ev.TextDelta(text="a"))
    log.append(ev.TextDelta(text="b"))

    collector = asyncio.create_task(_collect(log))
    await asyncio.sleep(0)  # let it drain the replay and block on the condition
    log.append(ev.TurnEnd(turn=1, stop_reason="end_turn"))
    log.close()

    assert await collector == ["text_delta", "text_delta", "turn_end"]


async def test_two_subscribers_see_the_same_events():
    log = TurnLog()
    first = asyncio.create_task(_collect(log))
    second = asyncio.create_task(_collect(log))
    await asyncio.sleep(0)
    log.append(ev.TextDelta(text="x"))
    log.close()
    assert await first == await second == ["text_delta"]


async def test_subscribing_to_a_closed_log_replays_and_ends():
    log = TurnLog()
    log.append(ev.Done(session_id=1))
    log.close()
    assert await _collect(log) == ["done"]


def test_append_after_close_is_an_error():
    log = TurnLog()
    log.close()
    try:
        log.append(ev.TextDelta(text="late"))
    except RuntimeError as exc:
        assert "closed" in str(exc)
    else:
        raise AssertionError("append after close must raise")
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && uv run pytest tests/test_turnlog.py -q`
Expected: FAIL — `ModuleNotFoundError: app.agent.turnlog`.

- [ ] **Step 3: Implement**

Create `backend/app/agent/turnlog.py`:

```python
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

        asyncio.get_running_loop().create_task(wake())

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
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && uv run pytest tests/test_turnlog.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/agent/turnlog.py backend/tests/test_turnlog.py
git commit -m "Keep a replayable log of a running turn"
```

---

### Task 3: `TurnRegistry` — the task that outlives the request

**Files:**
- Create: `backend/app/agent/turns.py`
- Test: `backend/tests/test_turn_registry.py`

**Interfaces:**
- Consumes: `TurnLog` (Task 2), `ev.TurnStarted` (Task 1), `ResearchSession.turn_status` (Task 1), `app.api.tasks.CANCEL_WAIT_S` (existing).
- Produces:
  - `class TurnAlreadyRunning(Exception)`
  - `@dataclass class RunningTurn: turn_id: str; session_id: int; started_at: datetime; log: TurnLog; task: asyncio.Task`
  - `class TurnRegistry` with `async start(*, session_id: int, session_factory, generator: AsyncIterator[AgentEvent], client: AsyncAnthropic | None, prompt: str, attachments: list[dict]) -> RunningTurn`; `get(session_id) -> RunningTurn | None`; `is_running(session_id) -> bool`; `running_ids() -> list[int]`; `async cancel(session_id) -> bool`; `async cancel_and_wait(session_id) -> bool`; `async drain() -> None` (cancel everything and wait, bounded).

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_turn_registry.py`:

```python
import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select

from app.agent import events as ev
from app.agent.turns import TurnAlreadyRunning, TurnRegistry
from app.db.models import ResearchSession


class ClosableClient:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def _new_session(session_factory) -> int:
    async with session_factory() as session:
        row = ResearchSession(title="t")
        session.add(row)
        await session.commit()
        return row.id


async def _status(session_factory, session_id: int) -> tuple[str, object]:
    async with session_factory() as session:
        row = (
            await session.execute(select(ResearchSession).where(ResearchSession.id == session_id))
        ).scalar_one()
        return row.turn_status, row.turn_started_at


async def slow_turn(steps: int, delay: float) -> AsyncIterator[ev.AgentEvent]:
    for index in range(steps):
        await asyncio.sleep(delay)
        yield ev.TextDelta(text=str(index))
    yield ev.Done(session_id=None)


async def test_start_runs_the_turn_to_completion_without_a_subscriber(session_factory):
    registry = TurnRegistry()
    session_id = await _new_session(session_factory)
    client = ClosableClient()

    turn = await registry.start(
        session_id=session_id,
        session_factory=session_factory,
        generator=slow_turn(3, 0.01),
        client=client,
        prompt="hi",
        attachments=[],
    )
    assert registry.is_running(session_id)
    assert (await _status(session_factory, session_id))[0] == "running"
    assert turn.log.events[0].type == "turn_started"

    await turn.task
    assert registry.get(session_id) is None
    assert not registry.is_running(session_id)
    assert client.closed
    assert (await _status(session_factory, session_id))[0] == "idle"
    assert [event.type for event in turn.log.events] == [
        "turn_started", "text_delta", "text_delta", "text_delta", "done",
    ]
    assert turn.log.closed


async def test_a_second_start_for_the_same_session_is_refused(session_factory):
    registry = TurnRegistry()
    session_id = await _new_session(session_factory)
    first = await registry.start(
        session_id=session_id, session_factory=session_factory,
        generator=slow_turn(5, 0.02), client=None, prompt="a", attachments=[],
    )
    with pytest.raises(TurnAlreadyRunning):
        await registry.start(
            session_id=session_id, session_factory=session_factory,
            generator=slow_turn(1, 0), client=None, prompt="b", attachments=[],
        )
    await registry.cancel_and_wait(session_id)
    assert first.task.done()


async def test_two_sessions_run_at_once(session_factory):
    registry = TurnRegistry()
    one = await _new_session(session_factory)
    two = await _new_session(session_factory)
    await registry.start(session_id=one, session_factory=session_factory,
                         generator=slow_turn(3, 0.02), client=None, prompt="a", attachments=[])
    await registry.start(session_id=two, session_factory=session_factory,
                         generator=slow_turn(3, 0.02), client=None, prompt="b", attachments=[])
    assert sorted(registry.running_ids()) == sorted([one, two])
    await registry.drain()
    assert registry.running_ids() == []


async def test_cancel_ends_the_log_with_cancelled_then_done_and_idles_the_row(session_factory):
    registry = TurnRegistry()
    session_id = await _new_session(session_factory)
    turn = await registry.start(session_id=session_id, session_factory=session_factory,
                                generator=slow_turn(50, 0.02), client=None, prompt="a", attachments=[])
    await asyncio.sleep(0.05)
    assert await registry.cancel(session_id) is True
    await asyncio.gather(turn.task, return_exceptions=True)
    types = [event.type for event in turn.log.events]
    assert types[-2:] == ["error", "done"]
    assert turn.log.events[-2].error_type == "cancelled"
    assert (await _status(session_factory, session_id))[0] == "idle"
    assert await registry.cancel(session_id) is False


async def test_a_generator_that_raises_ends_the_log_with_an_api_error(session_factory):
    async def broken() -> AsyncIterator[ev.AgentEvent]:
        yield ev.TextDelta(text="x")
        raise RuntimeError("boom")

    registry = TurnRegistry()
    session_id = await _new_session(session_factory)
    turn = await registry.start(session_id=session_id, session_factory=session_factory,
                                generator=broken(), client=None, prompt="a", attachments=[])
    await asyncio.gather(turn.task, return_exceptions=True)
    types = [event.type for event in turn.log.events]
    assert types == ["turn_started", "text_delta", "error", "done"]
    assert turn.log.events[2].error_type == "api_error"
    assert (await _status(session_factory, session_id))[0] == "idle"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && uv run pytest tests/test_turn_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: app.agent.turns`.

- [ ] **Step 3: Implement**

Create `backend/app/agent/turns.py`:

```python
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
                logger.warning("Could not idle session %s after its turn", session_id, exc_info=True)
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
```

Note `_finish` pops the entry in its own `finally`, so `get()` is `None` as soon as the task is done even if the status write failed.

- [ ] **Step 4: Run the tests**

Run: `cd backend && uv run pytest tests/test_turn_registry.py tests/test_turnlog.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/agent/turns.py backend/tests/test_turn_registry.py
git commit -m "Own a running turn in the process, not in the request"
```

---

### Task 4: Registry on the app, interrupted rows at startup, drain at shutdown

**Files:**
- Modify: `backend/app/main.py` (`create_app`, `lifespan`)
- Modify: `backend/app/api/deps.py` (append)
- Test: `backend/tests/test_app_lifespan.py` (create; look at how existing tests build an app with `app_factory` in `tests/conftest.py`)

**Interfaces:**
- Produces: `app.state.turn_registry: TurnRegistry` (created in `create_app`, so tests that skip the lifespan still have it); `mark_interrupted(session_factory) -> int` in `app/agent/turns.py` (module function: sets every `running` row to `interrupted`, returns the count); `TurnRegistryDep = Annotated[TurnRegistry, Depends(get_turn_registry)]` in `app/api/deps.py`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_app_lifespan.py`:

```python
from sqlalchemy import select

from app.agent.turns import TurnRegistry, mark_interrupted
from app.db.models import ResearchSession


async def test_mark_interrupted_flips_only_running_rows(session_factory):
    async with session_factory() as session:
        session.add_all(
            [
                ResearchSession(title="a", turn_status="running"),
                ResearchSession(title="b", turn_status="idle"),
                ResearchSession(title="c", turn_status="interrupted"),
            ]
        )
        await session.commit()

    assert await mark_interrupted(session_factory) == 1

    async with session_factory() as session:
        rows = (await session.execute(select(ResearchSession).order_by(ResearchSession.title))).scalars().all()
        assert [row.turn_status for row in rows] == ["interrupted", "idle", "interrupted"]


def test_create_app_installs_a_turn_registry(app):
    assert isinstance(app.state.turn_registry, TurnRegistry)
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && uv run pytest tests/test_app_lifespan.py -q`
Expected: FAIL — `ImportError: cannot import name 'mark_interrupted'`.

- [ ] **Step 3: Implement**

Append to `backend/app/agent/turns.py` (before `__all__`, and add it to `__all__`):

```python
async def mark_interrupted(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Startup: a row still ``running`` belongs to a process that died mid-turn."""
    async with session_factory() as session:
        result = await session.execute(
            update(ResearchSession)
            .where(ResearchSession.turn_status == "running")
            .values(turn_status="interrupted")
        )
        await session.commit()
        return int(result.rowcount or 0)
```

In `backend/app/main.py`:
- import `from app.agent.turns import TurnRegistry, mark_interrupted`;
- in `create_app`, next to the other `app.state.*` initialisations: `app.state.turn_registry = TurnRegistry()`;
- in `lifespan`, right after `await init_db(...)`:

```python
    interrupted = await mark_interrupted(app.state.session_factory)
    if interrupted:
        logger.warning("%d research turn(s) were running when the process last stopped; marked interrupted", interrupted)
```

- in the lifespan's `finally`, before the MCP manager close:

```python
        registry: TurnRegistry | None = getattr(app.state, "turn_registry", None)
        if registry is not None:
            await registry.drain()
```

Append to `backend/app/api/deps.py`:

```python
def get_turn_registry(request: Request) -> TurnRegistry:
    return request.app.state.turn_registry


TurnRegistryDep = Annotated[TurnRegistry, Depends(get_turn_registry)]
```

(import `TurnRegistry` from `app.agent.turns`; `Request`, `Annotated`, `Depends` are already imported there — check.)

- [ ] **Step 4: Run the tests**

Run: `cd backend && uv run pytest tests/test_app_lifespan.py -q && uv run pytest -q`
Expected: PASS; the full suite still green.

- [ ] **Step 5: Commit**

```bash
git add backend/app/main.py backend/app/api/deps.py backend/app/agent/turns.py backend/tests/test_app_lifespan.py
git commit -m "Install the turn registry and mark orphaned turns interrupted"
```

---

### Task 5: The API — 202 on POST, `GET /stream`, `GET /sessions/running`

**Files:**
- Modify: `backend/app/api/streaming.py` (append `stream_turn_log`)
- Modify: `backend/app/api/sessions.py:196-345` (`delete_session`, `cancel_turn`, `post_message`, remove `_stream_turn`) and add two routes
- Test: `backend/tests/test_api_sessions_stream.py` (create); update the three turn tests in `backend/tests/test_api_sessions.py` (`test_a_second_concurrent_turn_is_a_conflict`, `test_cancel_stops_a_running_turn_and_keeps_what_was_persisted`, `test_delete_during_a_running_turn_cancels_and_awaits_it_first`) and any other test that reads SSE off the POST.

**Interfaces:**
- Consumes: `TurnRegistryDep` (Task 4), `TurnAccepted`, `RunningSessions` (Task 1), `TurnLog.subscribe()` (Task 2).
- Produces: `stream_turn_log(request: Request, log: TurnLog) -> AsyncIterator[dict[str, str]]` (SSE frames; stops on disconnect without cancelling anything); routes `POST /api/sessions/{id}/messages` → 202 `TurnAccepted`; `GET /api/sessions/running` → `RunningSessions`; `GET /api/sessions/{id}/stream` → SSE or 204.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_api_sessions_stream.py` (reuse `create_session`, `use_script`, `with_key`, `parse_sse`, `payloads_for` from the existing sessions tests — import them from `tests.test_api_sessions` and `tests.sse_util` the way that file does):

```python
import asyncio

import httpx2
from httpx2 import ASGITransport

from app.api.deps import get_chat_client_factory
from fakes.anthropic import ScriptedAnthropic, turn_text, turn_tool_use
from tests.sse_util import event_names, parse_sse, payloads_for
from tests.test_api_sessions import create_session


def _http(app):
    return httpx2.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_post_returns_202_and_the_turn_finishes_with_no_one_listening(app, with_key):
    scripted = ScriptedAnthropic([turn_text("answer", delay_s=0.05)])
    app.dependency_overrides[get_chat_client_factory] = lambda: lambda _key: scripted

    async with _http(app) as http:
        session_id = await create_session(http)
        accepted = await http.post(f"/api/sessions/{session_id}/messages", json={"content": "q"})
        assert accepted.status_code == 202
        body = accepted.json()
        assert body["session_id"] == session_id and body["turn_id"] and body["started_at"]

        listed = (await http.get("/api/sessions/running")).json()
        assert listed == {"session_ids": [session_id]}

        # Nobody attaches. The turn must still complete and persist its answer.
        for _ in range(50):
            await asyncio.sleep(0.02)
            if not app.state.turn_registry.is_running(session_id):
                break
        detail = (await http.get(f"/api/sessions/{session_id}")).json()
        assert detail["session"]["turn_status"] == "idle"
        assert [m["kind"] for m in detail["messages"]] == ["user", "assistant"]
        assert (await http.get("/api/sessions/running")).json() == {"session_ids": []}


async def test_attaching_mid_turn_replays_then_tails_to_done(app, with_key):
    scripted = ScriptedAnthropic(
        [turn_tool_use([("search_feed_items", {"q": "x"})], text="looking", delay_s=0.05),
         turn_text("done", delay_s=0.05)]
    )
    app.dependency_overrides[get_chat_client_factory] = lambda: lambda _key: scripted

    async with _http(app) as http:
        session_id = await create_session(http)
        await http.post(f"/api/sessions/{session_id}/messages", json={"content": "q"})
        await asyncio.sleep(0.03)

        stream = await http.get(f"/api/sessions/{session_id}/stream")
        assert stream.status_code == 200
        names = event_names(stream.text)
        assert names[0] == "turn_started"
        assert payloads_for(stream.text, "turn_started")[0]["prompt"] == "q"
        assert "tool_use_start" in names and "text_delta" in names
        assert names[-1] == "done"


async def test_attaching_when_nothing_runs_is_a_204(app, with_key):
    async with _http(app) as http:
        session_id = await create_session(http)
        assert (await http.get(f"/api/sessions/{session_id}/stream")).status_code == 204
        assert (await http.get("/api/sessions/999/stream")).status_code == 404


async def test_a_detached_subscriber_does_not_stop_the_turn(app, with_key):
    scripted = ScriptedAnthropic([turn_text("slow", delay_s=0.2)])
    app.dependency_overrides[get_chat_client_factory] = lambda: lambda _key: scripted

    async with _http(app) as http:
        session_id = await create_session(http)
        await http.post(f"/api/sessions/{session_id}/messages", json={"content": "q"})
        # Open a stream and drop it almost immediately.
        async with http.stream("GET", f"/api/sessions/{session_id}/stream") as response:
            await response.aiter_raw().__anext__()
        assert app.state.turn_registry.is_running(session_id)
        await asyncio.gather(app.state.turn_registry.get(session_id).task, return_exceptions=True)
        detail = (await http.get(f"/api/sessions/{session_id}")).json()
        assert detail["messages"][-1]["kind"] == "assistant"


async def test_cancel_ends_a_subscribed_stream_with_cancelled_then_done(app, with_key):
    scripted = ScriptedAnthropic([turn_text("slow", delay_s=0.3)])
    app.dependency_overrides[get_chat_client_factory] = lambda: lambda _key: scripted

    async with _http(app) as http:
        session_id = await create_session(http)
        await http.post(f"/api/sessions/{session_id}/messages", json={"content": "q"})
        stream = asyncio.create_task(http.get(f"/api/sessions/{session_id}/stream"))
        await asyncio.sleep(0.05)
        assert (await http.post(f"/api/sessions/{session_id}/cancel")).json() == {"cancelled": True}
        response = await stream
        assert event_names(response.text)[-2:] == ["error", "done"]
        assert payloads_for(response.text, "error")[0]["type"] == "cancelled"


async def test_no_api_key_still_persists_the_question_and_streams_the_error(app):
    async with _http(app) as http:
        session_id = await create_session(http)
        accepted = await http.post(f"/api/sessions/{session_id}/messages", json={"content": "q"})
        assert accepted.status_code == 202
        stream = await http.get(f"/api/sessions/{session_id}/stream")
        # The log may already be closed and dropped: either the stream replays it
        # or the turn is gone and the transcript has the question.
        if stream.status_code == 200:
            assert payloads_for(stream.text, "error")[0]["message"] == "no API key configured"
        detail = (await http.get(f"/api/sessions/{session_id}")).json()
        assert detail["messages"][0]["kind"] == "user"
```

Update the three existing tests in `test_api_sessions.py` to the new contract: the POST no longer streams. For `test_a_second_concurrent_turn_is_a_conflict` the first POST returns 202 immediately, the second (while the fake is still slow) is 409, then `await registry.get(id).task`. For the cancel test, the events come from `GET /stream` opened after the POST. For the delete test, POST then DELETE while running; assert the row is gone and the registry is empty. Any other test that parsed SSE off the POST switches to `GET /stream`.

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && uv run pytest tests/test_api_sessions_stream.py -q`
Expected: FAIL — POST still returns 200 with a stream; `/stream` and `/running` are 404/422.

- [ ] **Step 3: Implement the stream helper**

Append to `backend/app/api/streaming.py`:

```python
async def stream_turn_log(request: Request, log: "TurnLog") -> AsyncIterator[dict[str, str]]:
    """Replay *log* and tail it as SSE frames, stopping when the client leaves.

    Leaving detaches only this subscriber — the turn keeps running. Disconnects
    are noticed between events, so a long silence is polled at
    ``DISCONNECT_POLL_S`` like the note-generation pump.
    """
    subscription = log.subscribe()
    try:
        while True:
            try:
                event = await asyncio.wait_for(subscription.__anext__(), timeout=DISCONNECT_POLL_S)
            except TimeoutError:
                if await request.is_disconnected():
                    return
                continue
            except StopAsyncIteration:
                return
            yield sse_frame(event)
    finally:
        await subscription.aclose()
```

(add `from app.agent.turnlog import TurnLog` under `TYPE_CHECKING`, or import it directly — there is no cycle.)

- [ ] **Step 4: Rewrite the chat routes**

In `backend/app/api/sessions.py`:

```python
@router.get("/sessions/running", response_model=RunningSessions)
async def running_sessions(registry: TurnRegistryDep) -> RunningSessions:
    """Which sessions have a turn in flight. Registry only — no database."""
    return RunningSessions(session_ids=sorted(registry.running_ids()))
```

This route MUST be declared **above** `GET /sessions/{session_id}` or FastAPI parses "running" as the id and answers 422.

```python
@router.post(
    "/sessions/{session_id}/messages",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TurnAccepted,
)
async def post_message(
    session_id: int,
    payload: MessageCreate,
    request: Request,
    session: DbSession,
    session_factory: SessionFactory,
    client_factory: ChatClientFactory,
    app_settings: AppSettings,
    registry: TurnRegistryDep,
) -> TurnAccepted:
    """Start one agent turn in the background; attach to ``/stream`` to watch it."""
    await _load_session(session, session_id)
    if registry.is_running(session_id):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="A turn is already running for this session.")

    resolved = await turn_settings(session, app_settings)
    user_content: list[dict[str, Any]] = [{"type": "text", "text": payload.content}]
    attachments = await _resolve_attachments(session, payload.attached_item_ids)
    user_content.extend(attachments)
    chips = await _attachment_chips(session, payload.attached_item_ids)

    if not resolved["api_key"]:
        message_id = await persistence.append_user_message(session_factory, session_id, user_content)
        generator = frames_as_events(
            ev.Error(error_type="api_error", message="no API key configured"),
            ev.Done(session_id=session_id, message_ids=[message_id]),
        )
        client = None
    else:
        registry_tools = ToolRegistry(await build_tool_providers(request, session, session_factory))
        client = client_factory(resolved["api_key"])
        generator = agent_runner.run(
            client=client,
            db_session_factory=session_factory,
            registry=registry_tools,
            session_id=session_id,
            user_content=user_content,
            model=resolved["model"],
            effort=resolved["effort"],
            thinking_display=resolved["thinking_display"],
            max_tool_turns=resolved["max_tool_turns"],
            system_extra=resolved["system_prompt_extra"],
        )

    try:
        turn = await registry.start(
            session_id=session_id,
            session_factory=session_factory,
            generator=generator,
            client=client,
            prompt=payload.content,
            attachments=chips,
        )
    except TurnAlreadyRunning:
        if client is not None:
            await client.close()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="A turn is already running for this session.") from None
    return TurnAccepted(turn_id=turn.turn_id, session_id=session_id, started_at=turn.started_at)


@router.get("/sessions/{session_id}/stream")
async def stream_session(
    session_id: int, request: Request, session: DbSession, registry: TurnRegistryDep
) -> Response:
    """Attach to the running turn: replay from its start, then follow it."""
    await _load_session(session, session_id)
    turn = registry.get(session_id)
    if turn is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return EventSourceResponse(stream_turn_log(request, turn.log), ping=SSE_PING_S, headers=SSE_HEADERS)
```

Add the two helpers next to `_resolve_attachments`:

```python
async def frames_as_events(*events: ev.AgentEvent) -> AsyncIterator[ev.AgentEvent]:
    for event in events:
        yield event


async def _attachment_chips(session: AsyncSession, item_ids: list[int]) -> list[dict[str, Any]]:
    """``{id, title, url}`` per attached item, for the turn's opening event."""
    if not item_ids:
        return []
    rows = (await session.execute(select(FeedItem).where(FeedItem.id.in_(item_ids)))).scalars().all()
    by_id = {row.id: row for row in rows}
    return [
        {"id": row.id, "title": row.title, "url": row.url}
        for item_id in item_ids
        if (row := by_id.get(item_id)) is not None
    ]
```

`cancel_turn` becomes `cancelled = await registry.cancel(session_id)`; `delete_session` calls `await registry.cancel_and_wait(session_id)` instead of the task registry. Delete `_stream_turn` and the now-unused imports (`pump_agent_events`, `frames`, `sse_frame`, `task_registry`); import `TurnAlreadyRunning`, `TurnRegistryDep`, `stream_turn_log`, `TurnAccepted`, `RunningSessions`. Update the module docstring: the turn is owned by the registry; the request returns at once.

- [ ] **Step 5: Run the tests**

Run: `cd backend && uv run pytest -q && uv run ruff check . && uv run ruff format --check app/api/sessions.py app/api/streaming.py app/agent/turns.py app/agent/turnlog.py`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/sessions.py backend/app/api/streaming.py backend/tests/test_api_sessions_stream.py backend/tests/test_api_sessions.py
git commit -m "Start a turn with a 202 and attach to it with a stream"
```

---

### Task 6: Backend docs

**Files:**
- Modify: `backend/CLAUDE.md` (endpoints table, SSE protocol section, module map, test count), `backend/app/agent/CLAUDE.md` (a "Turn ownership" section), root `CLAUDE.md` ("This app" bullet), `docs/DESIGN.md` (Research view / SSE paragraph gets an "Implementation notes" line).

- [ ] **Step 1: Write the doc changes**

`backend/CLAUDE.md`: in the endpoints table replace the POST row with `POST /sessions/{id}/messages → 202 TurnAccepted (409 running)`, add `GET /sessions/{id}/stream → SSE replay+tail, 204 when idle` and `GET /sessions/running → {session_ids}`; in the SSE section add the `turn_started` event and the sentence "A chat turn is owned by `app.agent.turns.TurnRegistry` (`app.state.turn_registry`), not by the request; `pump_agent_events` now serves only note generation."; module map rows for `agent/turns.py` and `agent/turnlog.py`; the `research_sessions.turn_status`/`turn_started_at` columns and the delete-the-dev-DB note; update the test count to the real number from `uv run pytest -q`.

`backend/app/agent/CLAUDE.md`: a section "Turn ownership (`turns.py`, `turnlog.py`)" — one entry per session, `TurnStarted` first, cancel is the only stop, `_finish` is shielded, `mark_interrupted` at startup, and the rule: **never** cancel a turn because a client went away.

Root `CLAUDE.md` "This app": `- A research turn is a session-owned task (`app.agent.turns`) that outlives the request; pages attach with GET /stream. The only refetch trigger for "is anything running" is window focus and the page's own events — still no poller.`

`docs/DESIGN.md`: in the Research/SSE paragraph an "Implementation notes" line: POST returns 202, the turn runs detached, `GET /stream` replays; `turn_status` on the session.

- [ ] **Step 2: Verify the numbers**

Run: `cd backend && uv run pytest -q | tail -1` and put that count in `backend/CLAUDE.md`.

- [ ] **Step 3: Commit**

```bash
git add backend/CLAUDE.md backend/app/agent/CLAUDE.md CLAUDE.md docs/DESIGN.md
git commit -m "Describe the detached turn in the guides"
```

---

### Task 7: Frontend API layer and the reducer's `turn_started`

**Files:**
- Modify: `frontend/src/api/chat.ts` (`ResearchSession`, new functions/types)
- Modify: `frontend/src/components/chat/liveTurn.ts` (`LiveAction`, reducer)
- Test: `frontend/src/components/chat/liveTurn.test.ts`, `frontend/src/api/chat.test.ts`

**Interfaces:**
- Produces in `api/chat.ts`:
  - `ResearchSession.turn_status: 'idle' | 'running' | 'interrupted'`, `ResearchSession.turn_started_at: string | null`
  - `interface TurnAccepted { turn_id: string; session_id: number; started_at: string }`
  - `startTurn(id: number, body: { content: string; attached_item_ids: number[] }): Promise<TurnAccepted>` (POST, expects 202)
  - `streamUrl(id: number): string` → `/api/sessions/${id}/stream`
  - `fetchRunningSessions(): Promise<{ session_ids: number[] }>`
  - `interface TurnStartedPayload { turn_id: string; session_id: number; prompt: string; attachments: TurnAttachment[]; started_at: string }`
  - `resendPayload(messages: ChatMessage[]): { content: string; attached_item_ids: number[] } | null` — the last user message's question text and its attached item ids (from `attachmentsFromMessage`).
- Produces in `liveTurn.ts`: reducer action `{ kind: 'attach'; token: number }` (marks the turn as attaching: `streaming: true`, `sessionId`, empty prompt) and the SSE case `turn_started` which fills `prompt`, `attachments`, `startedAt` (`Date.parse(payload.started_at + 'Z')` — server times are naive UTC), `sessionId`, and sets `activity: 'starting'`.

- [ ] **Step 1: Write the failing tests**

Append to `liveTurn.test.ts`:

```ts
describe('attaching to a running turn', () => {
  it('turn_started fills the turn the way start does', () => {
    let state = liveTurnReducer(emptyTurn, { kind: 'attach', sessionId: 12, token: 3 })
    expect(state.streaming).toBe(true)
    expect(state.prompt).toBeNull()
    state = liveTurnReducer(state, {
      kind: 'sse',
      token: 3,
      event: 'turn_started',
      payload: {
        turn_id: 'abc',
        session_id: 12,
        prompt: 'what happened?',
        attachments: [{ id: 3, title: 'An item', url: 'https://example.test/a' }],
        started_at: '2026-09-16T10:00:00',
      },
    })
    expect(state.prompt).toBe('what happened?')
    expect(state.attachments).toEqual([{ id: 3, title: 'An item', url: 'https://example.test/a' }])
    expect(state.startedAt).toBe(Date.parse('2026-09-16T10:00:00Z'))
    expect(state.sessionId).toBe(12)
    expect(state.activity).toBe('starting')
  })

  it('a turn_started from a stale token is ignored', () => {
    const state = liveTurnReducer(emptyTurn, { kind: 'attach', sessionId: 12, token: 3 })
    const next = liveTurnReducer(state, {
      kind: 'sse', token: 2, event: 'turn_started',
      payload: { turn_id: 'x', session_id: 12, prompt: 'p', attachments: [], started_at: '2026-09-16T10:00:00' },
    })
    expect(next).toBe(state)
  })
})
```

Append to `chat.test.ts`:

```ts
describe('resendPayload', () => {
  it('rebuilds the last question and its attachments', () => {
    const messages = [
      userMessage(1, 'first', []),
      assistantText(2, 'a'),
      userMessage(3, 'second', [{ id: 9, title: 'Item nine', url: 'https://example.test/9' }]),
    ]
    expect(resendPayload(messages)).toEqual({ content: 'second', attached_item_ids: [9] })
  })
  it('is null with no user message', () => {
    expect(resendPayload([])).toBeNull()
  })
})
```

(`userMessage`/`assistantText` are the builders already used in `chat.test.ts` for `groupTurns`; if the user builder has no attachment parameter, extend it: attachments are the `Attached feed items:` block the server appends — see `attachmentsFromMessage` for the exact shape and build it the same way.)

- [ ] **Step 2: Run to verify failure**

Run: `cd frontend && npx vitest run src/components/chat/liveTurn.test.ts src/api/chat.test.ts`
Expected: FAIL — unknown action kind `attach`, `resendPayload` not exported.

- [ ] **Step 3: Implement**

`api/chat.ts`:

```ts
export type TurnStatus = 'idle' | 'running' | 'interrupted'
// in ResearchSession:
//   turn_status: TurnStatus
//   turn_started_at: string | null

export interface TurnAccepted { turn_id: string; session_id: number; started_at: string }

export const startTurn = (
  id: number,
  body: { content: string; attached_item_ids: number[] },
): Promise<TurnAccepted> => request(`/api/sessions/${id}/messages`, { method: 'POST', body })

export const streamUrl = (id: number): string => `/api/sessions/${id}/stream`

export const fetchRunningSessions = (): Promise<{ session_ids: number[] }> =>
  request('/api/sessions/running')

export interface TurnStartedPayload {
  turn_id: string
  session_id: number
  prompt: string
  attachments: TurnAttachment[]
  started_at: string
}

/** What "Send again" re-sends after an interrupted turn: the last question as typed. */
export function resendPayload(
  messages: ChatMessage[],
): { content: string; attached_item_ids: number[] } | null {
  const last = [...messages].reverse().find((m) => m.kind === 'user')
  if (!last) return null
  return {
    content: questionFromMessage(last),
    attached_item_ids: attachmentsFromMessage(last).map((a) => a.id),
  }
}
```

(`request` is whatever helper `createSession`/`fetchSession` already use — match it; remove `messagesUrl` once nothing uses it.)

`liveTurn.ts`: add `| { kind: 'attach'; sessionId: number; token: number }` to `LiveAction`; reducer:

```ts
    case 'attach':
      // A page joining a turn it did not start. The prompt arrives in the
      // replayed `turn_started`; until then the turn is streaming with nothing
      // to show, which is what keeps the composer disabled and the progress
      // line honest.
      return { ...emptyTurn, token: action.token, sessionId: action.sessionId, streaming: true, activity: 'starting', startedAt: Date.now() }
```

and in the SSE switch, before `turn_start`:

```ts
    case 'turn_started': {
      const started = payload as TurnStartedPayload
      return {
        ...state,
        sessionId: started.session_id,
        prompt: started.prompt,
        attachments: started.attachments ?? [],
        startedAt: Date.parse(`${started.started_at}Z`),
        activity: 'starting',
      }
    }
```

The existing token check at the top of the `sse` case already ignores stale tokens.

- [ ] **Step 4: Run the tests**

Run: `cd frontend && npx vitest run && npm run build`
Expected: PASS, build clean (fix the `ResearchSession` fixtures in tests that now need the two new fields).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/chat.ts frontend/src/components/chat/liveTurn.ts frontend/src/components/chat/liveTurn.test.ts frontend/src/api/chat.test.ts
git commit -m "Teach the live turn to join a turn already running"
```

---

### Task 8: ChatPage — post then attach, attach on load, detach on leave

**Files:**
- Modify: `frontend/src/pages/ChatPage.tsx` (`abandonTurn`, `send`, new `attach`, an effect on `detail.data`)

**Interfaces:**
- Consumes: `startTurn`, `streamUrl`, reducer `attach` + `turn_started` (Task 7).
- Produces: `attach(id: number, token: number): Promise<void>` (opens the stream, feeds the reducer, runs the same `finally` as today); `abandonTurn()` no longer takes a session id and no longer calls `cancelTurn`.

- [ ] **Step 1: Restructure `send` and add `attach`**

Replace the body of `send` after `dispatch({ kind: 'start', ... })` with a call to `attach`, and make `attach` the single owner of the stream:

```ts
  const attach = useCallback(
    async (id: number, token: number) => {
      const mine = () => turnSeq.current === token
      const controller = new AbortController()
      abort.current = controller
      try {
        await streamSSE({
          url: streamUrl(id),
          method: 'GET',
          signal: controller.signal,
          onEvent: ({ event, data }) => {
            let payload: unknown = null
            try { payload = JSON.parse(data) } catch { return }
            dispatch({ kind: 'sse', event, payload, token })
          },
        })
      } catch (error) {
        if (controller.signal.aborted) {
          // Detached, not stopped: the turn goes on server-side. Nothing to show.
          return
        }
        if (error instanceof SSEHttpError && error.status === 204) {
          // Nothing running any more — the transcript has the answer.
        } else {
          const payload: ErrorPayload = {
            type: error instanceof SSEHttpError ? 'api_error' : 'connection',
            message: error instanceof Error ? error.message : String(error),
            category: null,
          }
          dispatch({ kind: 'failed', error: payload, token })
        }
      } finally {
        if (mine()) abort.current = null
        await queryClient.invalidateQueries({ queryKey: sessionQueryKey(id) })
        await queryClient.invalidateQueries({ queryKey: sessionsQueryKey })
        await queryClient.invalidateQueries({ queryKey: runningSessionsKey })
        if (mine()) dispatch({ kind: 'settle', token })
      }
    },
    [queryClient],
  )
```

`streamSSE` needs a `method` option (`'POST' | 'GET'`, default `'POST'`; no body for GET) and must treat a 204 as an `SSEHttpError(204, 'nothing running')` before reading the body — add both to `lib/sse.ts` with a unit test in `sse.test.ts` (a `Response` with status 204 rejects with status 204; a GET sends no body).

`send` becomes: abandon → create session if needed → `const accepted = await startTurn(id, { content: text, attached_item_ids: itemIds })` (a 409 becomes a `failed` with `api_error`, as today) → `dispatch({ kind: 'start', ..., startedAt: Date.parse(accepted.started_at + 'Z'), token })` → `invalidate runningSessionsKey` → `await attach(id, token)`.

`abandonTurn` becomes:

```ts
  const abandonTurn = useCallback(() => {
    turnSeq.current += 1
    abort.current?.abort()
    abort.current = null
    // Detach only. The turn is the session's, not this page's: it keeps running
    // and whoever opens the session next attaches to it. Stop is the one
    // deliberate cancel.
  }, [])
```

Update every call site (`newChat`, drawer `onOpen`, `remove.onSuccess`, the foreign-session effect, `send`'s prologue). `remove` (delete) keeps working because the server's DELETE cancels and waits.

- [ ] **Step 2: Attach on load**

After the `detail` query:

```ts
  // Joining a turn this page did not start: a reload, a return from the Inbox,
  // the drawer, a second tab. Keyed on the session and its status so a settle
  // (status back to idle) does not re-attach, and a new turn started elsewhere
  // (status running again) does.
  const turnStatus = detail.data?.session.turn_status ?? null
  useEffect(() => {
    if (sessionId === null || turnStatus !== 'running') return
    if (live.streaming && live.sessionId === sessionId) return // this page started it
    turnSeq.current += 1
    const token = turnSeq.current
    dispatch({ kind: 'attach', sessionId, token })
    void attach(sessionId, token)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `live` is read, not depended on
  }, [sessionId, turnStatus, attach])
```

(oxlint config may not have that rule; keep the intent comment and drop the disable line if it complains about an unknown rule.)

- [ ] **Step 3: Build, lint, and hand-check the flows**

Run: `cd frontend && npm run build && npm run lint && npx vitest run`
Expected: all clean. Then, with the backend from Tasks 1–5 running on a spare port (`PORT=8012 uv run python -m app` from this worktree's `backend/` with `DB_PATH` pointing at a scratch copy — never the user's `backend/data/app.db`) and `PORT=8012 npx vite --port 5174`, confirm in a browser: send → reload mid-turn → the turn is on screen with its steps and progress; Inbox and back → still there; Stop ends it.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/ChatPage.tsx frontend/src/lib/sse.ts frontend/src/lib/sse.test.ts
git commit -m "Let a turn outlive the page: post, attach, and only detach on leave"
```

---

### Task 9: The interrupted notice and "Send again"

**Files:**
- Create: `frontend/src/components/chat/InterruptedNotice.tsx`
- Modify: `frontend/src/pages/ChatPage.tsx` (render it; a `resend` callback)
- Test: `frontend/src/api/chat.test.ts` (`resendPayload`, already in Task 7)

**Interfaces:**
- Consumes: `resendPayload` (Task 7), `send` (Task 8).
- Produces: `<InterruptedNotice onResend={() => void} busy={boolean} />`.

- [ ] **Step 1: The component**

```tsx
import Button from '../ui/Button'
import Icon from '../ui/Icon'

export default function InterruptedNotice({ onResend, busy }: { onResend: () => void; busy: boolean }) {
  return (
    <div className="flex flex-col gap-2 rounded-[12px] border border-line bg-panel px-4 py-3">
      <div className="flex items-center gap-2 text-[13px] font-medium text-ink">
        <Icon name="close" size={13} className="text-faint" />
        Interrupted
      </div>
      <p className="m-0 text-[12.5px] text-muted">
        This research was interrupted by a backend restart. Anything already written is kept.
      </p>
      <div>
        <Button variant="primary" onClick={onResend} disabled={busy}>Send again</Button>
      </div>
    </div>
  )
}
```

(match the `Button` props and the `TurnError` component's look — read `TurnError.tsx` and use the same container classes.)

- [ ] **Step 2: Wire it**

In `ChatPage`, under the last stored turn when `turnStatus === 'interrupted' && !live.streaming`:

```tsx
  const resend = useCallback(() => {
    const payload = resendPayload(messages ?? [])
    if (payload) void send(payload.content, payload.attached_item_ids)
  }, [messages, send])
```

`send` gains an optional second argument `attachedItemIds?: number[]` that wins over the picker's `attached` when given. Sending clears the state because the server writes `running` and then `idle`.

- [ ] **Step 3: Verify**

Run: `cd frontend && npm run build && npm run lint && npx vitest run`. Manually: set a session's `turn_status` to `interrupted` in the scratch DB (`sqlite3 ... "update research_sessions set turn_status='interrupted' where id=…"`), open it, see the notice, press Send again, watch it run.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/chat/InterruptedNotice.tsx frontend/src/pages/ChatPage.tsx
git commit -m "Tell the user when a restart cut a turn short, and let them send it again"
```

---

### Task 10: Indicators — rail dot, drawer mark, header meta

**Files:**
- Create: `frontend/src/lib/useRunningTurns.ts`
- Modify: `frontend/src/components/ui/Rail.tsx`, `frontend/src/components/ui/AppShell.tsx` (pass the flag), `frontend/src/components/chat/HistoryDrawer.tsx`, `frontend/src/pages/ChatPage.tsx` (header meta)
- Test: `frontend/src/lib/useRunningTurns.test.ts` (the pure `runningHeaderMeta`)

**Interfaces:**
- Produces: `runningSessionsKey = ['sessions', 'running']` (in `api/chat.ts`, so Task 8's invalidation compiles — add it there in Task 8 if not yet present); `useRunningTurns(): Set<number>` (TanStack query on `fetchRunningSessions`, `refetchOnWindowFocus: true`, `staleTime: 5_000`, no interval); `runningHeaderMeta(startedAt: string | null, now: number): string` → `running · 1m 05s` using `formatElapsed` from `liveTurn.ts`.

- [ ] **Step 1: Write the failing test**

```ts
import { runningHeaderMeta } from './useRunningTurns'

it('formats the running meta from the server start time', () => {
  const started = '2026-09-16T10:00:00'
  expect(runningHeaderMeta(started, Date.parse('2026-09-16T10:01:05Z'))).toBe('running · 1m 05s')
  expect(runningHeaderMeta(null, 0)).toBe('')
})
```

- [ ] **Step 2: Implement**

`lib/useRunningTurns.ts`:

```ts
import { useQuery } from '@tanstack/react-query'
import { fetchRunningSessions, runningSessionsKey } from '../api/chat'
import { formatElapsed } from '../components/chat/liveTurn'

export function useRunningTurns(): Set<number> {
  const query = useQuery({
    queryKey: runningSessionsKey,
    queryFn: fetchRunningSessions,
    // Focus and the page's own invalidations are the only triggers: no interval.
    refetchOnWindowFocus: true,
    staleTime: 5_000,
  })
  return new Set(query.data?.session_ids ?? [])
}

export function runningHeaderMeta(startedAt: string | null, now: number): string {
  if (!startedAt) return ''
  const started = Date.parse(`${startedAt}Z`)
  if (Number.isNaN(started)) return ''
  return `running · ${formatElapsed(Math.max(0, now - started))}`
}
```

`Rail`: a `busyPages: Set<PageKey>` prop (AppShell computes `useRunningTurns().size > 0 ? new Set(['research']) : new Set()`); a nav item whose page is in the set renders a 6 px dot (`bg-accent-btn`, `rounded-full`, absolutely positioned at the icon's top-right) with `aria-label="A research turn is running"`.

`HistoryDrawer`: a `running: Set<number>` prop; a running row shows `<Icon name="spinner" size={11} />` before its date and the date text reads `running`.

`ChatPage` header meta: when `turnStatus === 'running'` use `runningHeaderMeta(session.turn_started_at, useElapsedNow())` — reuse `useElapsed` from `lib/useElapsed.ts` (it already ticks once a second from a start timestamp; if its API returns a formatted string, add a sibling `useNow(tickMs)` there instead of duplicating the interval). Refetch `runningSessionsKey` when the live turn starts/settles (Task 8 already invalidates in `attach`'s `finally` and after `startTurn`).

- [ ] **Step 3: Verify**

Run: `cd frontend && npx vitest run && npm run build && npm run lint`. In the browser on the scratch stack: start a turn, go to Inbox (dot on Research), open the drawer from another session (spinner on the running row), come back (header shows `running · Ns`), let it finish (dot gone on the next focus/refetch).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/useRunningTurns.ts frontend/src/lib/useRunningTurns.test.ts frontend/src/components/ui/Rail.tsx frontend/src/components/ui/AppShell.tsx frontend/src/components/chat/HistoryDrawer.tsx frontend/src/pages/ChatPage.tsx frontend/src/api/chat.ts
git commit -m "Show where a turn is running: the rail, the drawer, the header"
```

---

### Task 11: Frontend docs

**Files:**
- Modify: `frontend/CLAUDE.md` (API layer: `startTurn`/`streamUrl`/`fetchRunningSessions`; live turn: `attach` + `turn_started`, "leaving detaches, Stop cancels"; the no-poller rule for `useRunningTurns`; test counts), `docs/DESIGN.md` (Research view bullet: reload/navigation-safe turns).

- [ ] **Step 1: Write and verify counts**

Run `cd frontend && npx vitest run | grep -E "Test Files|Tests"` and put the numbers in.

- [ ] **Step 2: Commit**

```bash
git add frontend/CLAUDE.md docs/DESIGN.md
git commit -m "Describe attaching to a running turn in the frontend guide"
```

---

## Self-review

- **Spec coverage:** lifecycle → Tasks 3–5; log → Task 2; session state → Tasks 1, 4; API → Task 5; frontend send/attach/leave → Tasks 7–8; interrupted → Task 9; indicators → Task 10; docs → Tasks 6, 11; the three small items are the separate `fix/chat-drawer-polish` branch (not in this plan on purpose).
- **Placeholders:** none; every code step has its content. The "match the existing helper" notes point at named functions in named files.
- **Type consistency:** `TurnRegistry.start(...)` keyword names are identical in Tasks 3 and 5; `runningSessionsKey` is defined in `api/chat.ts` and used by Tasks 8 and 10; `turn_started` payload keys match `ev.TurnStarted.to_sse()` (`turn_id`, `session_id`, `prompt`, `attachments`, `started_at`).
