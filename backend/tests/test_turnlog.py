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
