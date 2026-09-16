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
        "turn_started",
        "text_delta",
        "text_delta",
        "text_delta",
        "done",
    ]
    assert turn.log.closed


async def test_a_second_start_for_the_same_session_is_refused(session_factory):
    registry = TurnRegistry()
    session_id = await _new_session(session_factory)
    first = await registry.start(
        session_id=session_id,
        session_factory=session_factory,
        generator=slow_turn(5, 0.02),
        client=None,
        prompt="a",
        attachments=[],
    )
    with pytest.raises(TurnAlreadyRunning):
        await registry.start(
            session_id=session_id,
            session_factory=session_factory,
            generator=slow_turn(1, 0),
            client=None,
            prompt="b",
            attachments=[],
        )
    await registry.cancel_and_wait(session_id)
    assert first.task.done()


async def test_two_sessions_run_at_once(session_factory):
    registry = TurnRegistry()
    one = await _new_session(session_factory)
    two = await _new_session(session_factory)
    await registry.start(
        session_id=one,
        session_factory=session_factory,
        generator=slow_turn(3, 0.02),
        client=None,
        prompt="a",
        attachments=[],
    )
    await registry.start(
        session_id=two,
        session_factory=session_factory,
        generator=slow_turn(3, 0.02),
        client=None,
        prompt="b",
        attachments=[],
    )
    assert sorted(registry.running_ids()) == sorted([one, two])
    await registry.drain()
    assert registry.running_ids() == []


async def test_cancel_ends_the_log_with_cancelled_then_done_and_idles_the_row(session_factory):
    registry = TurnRegistry()
    session_id = await _new_session(session_factory)
    turn = await registry.start(
        session_id=session_id,
        session_factory=session_factory,
        generator=slow_turn(50, 0.02),
        client=None,
        prompt="a",
        attachments=[],
    )
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
    turn = await registry.start(
        session_id=session_id,
        session_factory=session_factory,
        generator=broken(),
        client=None,
        prompt="a",
        attachments=[],
    )
    await asyncio.gather(turn.task, return_exceptions=True)
    types = [event.type for event in turn.log.events]
    assert types == ["turn_started", "text_delta", "error", "done"]
    assert turn.log.events[2].error_type == "api_error"
    assert (await _status(session_factory, session_id))[0] == "idle"
