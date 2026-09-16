import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.agent import events as ev
from app.agent import turns as turns_module
from app.agent.turns import RunningTurn, TurnAlreadyRunning, TurnRegistry, TurnStartFailed
from app.db.models import ResearchSession


class ClosableClient:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class SlowClosingClient:
    """A client whose ``close`` takes long enough for a second cancel to land."""

    def __init__(self, delay: float = 0.05) -> None:
        self.closed = False
        self._delay = delay

    async def close(self) -> None:
        await asyncio.sleep(self._delay)
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


async def _updated_at(session_factory, session_id: int):
    async with session_factory() as session:
        row = (
            await session.execute(select(ResearchSession).where(ResearchSession.id == session_id))
        ).scalar_one()
        return row.updated_at


async def done_then_waits(delay: float) -> AsyncIterator[ev.AgentEvent]:
    """Says ``done`` and keeps the generator alive, as the real runner does."""
    yield ev.TextDelta(text="x")
    yield ev.Done(session_id=None)
    await asyncio.sleep(delay)


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
    # And the turn lets go of it: the turn itself is kept for half a minute so
    # its log can still be replayed, and the client holds the API key.
    assert turn.client is None
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
    # The swallow is deliberate and Tasks 4/5 depend on it: a cancelled turn ends
    # *normally*, and its end is read off the log, never off the task.
    assert turn.task.cancelled() is False
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


# ------------------------------------------------- one turn per session, really


async def test_two_concurrent_starts_leave_exactly_one_turn(session_factory):
    """The check and the registration have to be one atomic step.

    With a plain check, both callers passed it — the second turn evicted the
    first, which then ran on unreachable: Stop could not stop it, shutdown could
    not drain it, and it kept writing into the same transcript.
    """
    registry = TurnRegistry()
    session_id = await _new_session(session_factory)
    first, second = slow_turn(5, 0.02), slow_turn(5, 0.02)

    results = await asyncio.gather(
        registry.start(
            session_id=session_id,
            session_factory=session_factory,
            generator=first,
            client=None,
            prompt="a",
            attachments=[],
        ),
        registry.start(
            session_id=session_id,
            session_factory=session_factory,
            generator=second,
            client=None,
            prompt="b",
            attachments=[],
        ),
        return_exceptions=True,
    )

    started = [result for result in results if isinstance(result, RunningTurn)]
    refused = [result for result in results if isinstance(result, TurnAlreadyRunning)]
    assert len(started) == 1 and len(refused) == 1
    assert registry.get(session_id) is started[0]
    assert registry.running_ids() == [session_id]

    await registry.cancel_and_wait(session_id)
    await asyncio.gather(first.aclose(), second.aclose(), return_exceptions=True)


async def test_a_failed_status_write_starts_nothing(session_factory, monkeypatch):
    """The row says ``running`` or there is no turn — never a task with neither."""

    async def boom(*_args, **_kwargs):
        raise OperationalError("UPDATE research_sessions", {}, Exception("database is locked"))

    monkeypatch.setattr(turns_module, "_set_status", boom)
    registry = TurnRegistry()
    session_id = await _new_session(session_factory)
    generator = slow_turn(1, 0)

    with pytest.raises(TurnStartFailed):
        await registry.start(
            session_id=session_id,
            session_factory=session_factory,
            generator=generator,
            client=None,
            prompt="a",
            attachments=[],
        )

    assert registry.get(session_id) is None
    assert registry.running_ids() == []
    await generator.aclose()


# --------------------------------------------------------- ending a turn once


async def test_a_second_cancel_during_the_cleanup_still_finishes_the_turn(session_factory):
    """Stop, then Delete — or Stop, then shutdown.

    ``shield`` protects the cleanup but does not keep the task attached to it, so
    the second cancel used to finish the task while the cleanup ran on as an
    orphan: ``drain()`` returned, the engine went away, and the row stayed
    ``running`` for a turn that had been stopped cleanly.
    """
    registry = TurnRegistry()
    session_id = await _new_session(session_factory)
    client = SlowClosingClient()
    turn = await registry.start(
        session_id=session_id,
        session_factory=session_factory,
        generator=slow_turn(50, 0.02),
        client=client,
        prompt="a",
        attachments=[],
    )
    await asyncio.sleep(0.05)

    assert await registry.cancel(session_id) is True
    await asyncio.sleep(0.01)  # the cleanup is now inside client.close()
    turn.task.cancel()
    await asyncio.gather(turn.task, return_exceptions=True)

    assert client.closed is True
    assert turn.log.closed is True
    assert (await _status(session_factory, session_id))[0] == "idle"
    assert registry.running_ids() == []


async def test_a_turn_cancelled_before_it_runs_is_still_closed_out(session_factory):
    """``create_task`` does not run the coroutine until the next loop pass.

    Cancelled before that pass, ``_drive`` never executes — so nothing closed the
    log (a subscriber waited for a ``done`` that could not come) and nothing
    idled the row (the session showed as interrupted after every restart).
    """
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

    assert await registry.cancel_and_wait(session_id) is True

    assert turn.log.closed is True
    assert [event.type for event in turn.log.events] == ["turn_started", "error", "done"]
    assert turn.log.events[1].error_type == "cancelled"
    assert (await _status(session_factory, session_id))[0] == "idle"
    assert registry.running_ids() == []


async def test_cancelling_a_turn_that_never_ran_idles_it_too(session_factory):
    """The same hole through ``POST /cancel``, which does not wait for the task."""
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

    assert await registry.cancel(session_id) is True
    for _ in range(50):
        await asyncio.sleep(0.01)
        if turn.log.closed:
            break

    assert turn.log.closed is True
    assert turn.log.events[-1].type == "done"
    assert (await _status(session_factory, session_id))[0] == "idle"


async def test_a_cancel_after_done_does_not_add_a_second_ending(session_factory):
    """A turn that already said ``done`` was not stopped in any way the user sees."""

    async def done_then_waits():
        yield ev.Done(session_id=None)
        await asyncio.sleep(5)

    registry = TurnRegistry()
    session_id = await _new_session(session_factory)
    turn = await registry.start(
        session_id=session_id,
        session_factory=session_factory,
        generator=done_then_waits(),
        client=None,
        prompt="a",
        attachments=[],
    )
    await asyncio.sleep(0.02)

    turn.task.cancel()
    await asyncio.gather(turn.task, return_exceptions=True)

    assert [event.type for event in turn.log.events] == ["turn_started", "done"]
    assert turn.log.closed is True


async def test_the_registry_forgets_a_turn_before_its_subscriber_sees_done(
    session_factory, monkeypatch
):
    """``done`` is the moment the page may send the next message.

    The bookkeeping that follows it — closing the client, idling the row — used to
    happen first, so that next message met a 409 for a turn that was over.
    """
    original = turns_module._set_status

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.05)
        await original(*args, **kwargs)

    monkeypatch.setattr(turns_module, "_set_status", slow)
    registry = TurnRegistry()
    session_id = await _new_session(session_factory)
    turn = await registry.start(
        session_id=session_id,
        session_factory=session_factory,
        # It must await *after* ``done``. Today's runner does not — it yields
        # ``Done`` as its last statement — but a generator that ends there hands
        # control straight to ``_drive``'s ``finally``, which forgets the turn
        # too, and the pre-``done`` forget (the fix itself) could then be deleted
        # with this test still green. So the generator models a runner that
        # *could* await there, and pins the forget that is load-bearing.
        generator=done_then_waits(0.5),
        client=None,
        prompt="a",
        attachments=[],
    )

    async for event in turn.log.subscribe():
        if isinstance(event, ev.Done):
            break

    assert registry.is_running(session_id) is False
    assert registry.running_ids() == []
    turn.task.cancel()
    await asyncio.gather(turn.task, return_exceptions=True)


async def test_a_turn_does_not_reorder_the_session_list(session_factory):
    """``updated_at`` orders the sidebar; a turn's own status writes must not move it."""
    registry = TurnRegistry()
    session_id = await _new_session(session_factory)
    before = await _updated_at(session_factory, session_id)

    turn = await registry.start(
        session_id=session_id,
        session_factory=session_factory,
        generator=slow_turn(2, 0.01),
        client=None,
        prompt="a",
        attachments=[],
    )
    assert await _updated_at(session_factory, session_id) == before

    await turn.task
    assert await _updated_at(session_factory, session_id) == before


# ------------------------------------------------- the just-finished turn's log


async def test_a_finished_turn_stays_replayable_for_a_short_while(session_factory, monkeypatch):
    """A turn can be over before the page that started it asks to watch it.

    An error-only turn is three events long, so it finishes inside the POST's own
    round trip; with nothing kept, ``GET /stream`` answered 204 and the error was
    never shown. The log outlives the registration for ``RECENT_TURN_S``.
    """
    registry = TurnRegistry()
    session_id = await _new_session(session_factory)
    turn = await registry.start(
        session_id=session_id,
        session_factory=session_factory,
        generator=slow_turn(1, 0.01),
        client=None,
        prompt="a",
        attachments=[],
    )
    await turn.task

    assert registry.get(session_id) is None
    assert registry.recent(session_id) is turn
    assert turn.log.closed is True

    monkeypatch.setattr(turns_module, "RECENT_TURN_S", -1.0)
    assert registry.recent(session_id) is None


async def test_starting_a_turn_drops_the_previous_one_from_the_recent_cache(session_factory):
    """One entry per session, and the new turn is what ``/stream`` must find."""
    registry = TurnRegistry()
    session_id = await _new_session(session_factory)
    first = await registry.start(
        session_id=session_id,
        session_factory=session_factory,
        generator=slow_turn(1, 0.01),
        client=None,
        prompt="a",
        attachments=[],
    )
    await first.task
    assert registry.recent(session_id) is first

    second = await registry.start(
        session_id=session_id,
        session_factory=session_factory,
        generator=slow_turn(1, 0.01),
        client=None,
        prompt="b",
        attachments=[],
    )
    assert registry.recent(session_id) is None
    assert registry.get(session_id) is second
    await second.task


async def test_a_stale_recent_entry_is_swept_when_another_turn_finishes(
    session_factory, monkeypatch
):
    """Nothing asks about most sessions again, and their logs are not free.

    ``recent()`` drops an expired entry only for the session it is asked about,
    so a session whose turn finished and that is never streamed again kept its
    whole ``TurnLog`` for the life of the process. Every finish sweeps.
    """
    registry = TurnRegistry()
    forgotten = await _new_session(session_factory)
    first = await registry.start(
        session_id=forgotten,
        session_factory=session_factory,
        generator=slow_turn(1, 0.01),
        client=None,
        prompt="a",
        attachments=[],
    )
    await first.task
    assert registry.recent(forgotten) is first

    # Age the first entry past the window, then finish a turn in *another*
    # session — the one event that has to notice.
    monkeypatch.setattr(turns_module, "RECENT_TURN_S", -1.0)
    other = await _new_session(session_factory)
    second = await registry.start(
        session_id=other,
        session_factory=session_factory,
        generator=slow_turn(1, 0.01),
        client=None,
        prompt="b",
        attachments=[],
    )
    await second.task

    # Back inside the window: an entry still there would now be served again.
    monkeypatch.setattr(turns_module, "RECENT_TURN_S", 30.0)
    assert registry.recent(forgotten) is None
    assert registry.recent(other) is second


async def test_a_late_finish_does_not_replace_a_newer_turn(session_factory):
    """``_finish`` must not hand ``/stream`` the *previous* turn's log.

    A turn is forgotten at its ``done``, before its own cleanup runs, so the
    next turn can start, run and finish while the first one is still on its way
    out. The write to the recent cache is guarded the way every other late-turn
    write is.
    """
    registry = TurnRegistry()
    session_id = await _new_session(session_factory)
    first = await registry.start(
        session_id=session_id,
        session_factory=session_factory,
        # Says ``done`` — which forgets it — and then lingers.
        generator=done_then_waits(0.3),
        client=None,
        prompt="a",
        attachments=[],
    )
    await asyncio.sleep(0.05)
    assert registry.is_running(session_id) is False

    second = await registry.start(
        session_id=session_id,
        session_factory=session_factory,
        generator=slow_turn(1, 0.01),
        client=None,
        prompt="b",
        attachments=[],
    )
    await second.task
    assert registry.recent(session_id) is second

    # Now let the first turn finish, long after the second one did.
    await first.task
    assert registry.recent(session_id) is second


async def swallows_one_cancel(delay: float) -> AsyncIterator[ev.AgentEvent]:
    """A turn that ignores the first cancel — the case the bounded waits exist for."""
    swallowed = False
    while True:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            if swallowed:
                raise
            swallowed = True
        yield ev.TextDelta(text="x")


async def test_drain_is_bounded_by_one_cancel_wait_not_two(session_factory, monkeypatch):
    """Shutdown gets ``CANCEL_WAIT_S`` in total, not once per wait.

    ``drain`` waits twice — on the tasks, then on the cleanups they left behind —
    and each wait used to start its own ten seconds. uvicorn's own graceful
    timeout can be shorter than the sum, in which case the second wait is the one
    that gets killed and the shutdown it was protecting never happens.
    """
    monkeypatch.setattr(turns_module, "CANCEL_WAIT_S", 0.1)
    timeouts: list[float | None] = []
    real_wait = asyncio.wait

    async def recording_wait(aws, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        return await real_wait(aws, **kwargs)

    monkeypatch.setattr(asyncio, "wait", recording_wait)

    registry = TurnRegistry()
    # One turn that will not stop, so the wait on the tasks times out...
    stubborn_id = await _new_session(session_factory)
    stubborn = await registry.start(
        session_id=stubborn_id,
        session_factory=session_factory,
        generator=swallows_one_cancel(0.02),
        client=None,
        prompt="a",
        attachments=[],
    )
    # ...and one already cancelled turn parked in a slow ``close``, so the wait on
    # the cleanups it left in ``_finishing`` times out too.
    closing_id = await _new_session(session_factory)
    closing = await registry.start(
        session_id=closing_id,
        session_factory=session_factory,
        generator=slow_turn(50, 0.02),
        client=SlowClosingClient(0.5),
        prompt="b",
        attachments=[],
    )
    await asyncio.sleep(0.02)
    assert await registry.cancel(closing_id) is True
    await asyncio.sleep(0.02)  # the cleanup is now inside client.close()

    await registry.drain()

    assert len(timeouts) == 2  # both waits ran...
    assert sum(timeouts) <= turns_module.CANCEL_WAIT_S  # ...sharing one deadline

    stubborn.task.cancel()
    await asyncio.gather(stubborn.task, closing.task, return_exceptions=True)
