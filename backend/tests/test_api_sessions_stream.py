"""The detached turn: a 202 that starts it, a stream that watches it.

The POST no longer carries the answer. It starts a turn the session owns and
returns; anyone who wants to watch it attaches to ``GET /stream``, which replays
the turn from its first event and then tails it. Leaving the stream detaches one
subscriber and nothing else — only ``POST /cancel`` stops a turn.
"""

from __future__ import annotations

import asyncio

import httpx2
import pytest
from fakes.anthropic import ScriptedAnthropic, turn_text, turn_tool_use
from httpx2 import ASGITransport
from sse_util import event_names, payloads_for
from test_api_sessions import create_session, finish_turn

from app.agent import events as ev
from app.agent import turns
from app.agent.turnlog import TurnLog
from app.api import streaming
from app.api.deps import get_chat_client_factory
from app.services import settings as settings_service


@pytest.fixture
async def with_key(db_session):
    await settings_service.set_value(db_session, "anthropic_api_key", "sk-ant-test")
    await db_session.commit()


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
        # Polled on the row rather than on the registry: a turn is forgotten the
        # moment it says ``done``, a breath before its row is idled.
        for _ in range(50):
            await asyncio.sleep(0.02)
            detail = (await http.get(f"/api/sessions/{session_id}")).json()
            if detail["session"]["turn_status"] == "idle":
                break
        assert detail["session"]["turn_status"] == "idle"
        assert [m["kind"] for m in detail["messages"]] == ["user", "assistant"]
        assert (await http.get("/api/sessions/running")).json() == {"session_ids": []}


async def test_running_is_a_route_not_a_session_id(app):
    """``/sessions/running`` must be declared above ``/sessions/{id}``.

    Declared the other way round FastAPI parses "running" as the id and answers
    422, and the sidebar's "what is in flight" query silently never works.
    """
    async with _http(app) as http:
        response = await http.get("/api/sessions/running")
        assert response.status_code == 200
        assert response.json() == {"session_ids": []}


async def test_attaching_mid_turn_replays_then_tails_to_done(app, with_key):
    scripted = ScriptedAnthropic(
        [
            turn_tool_use([("search_feed_items", {"q": "x"})], text="looking", delay_s=0.05),
            turn_text("done", delay_s=0.05),
        ]
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
    """The old contract cancelled the turn a second after the browser left.

    Now the subscriber is the only thing that ends: the turn keeps running and
    still persists its answer, which is the entire point of the rework.
    """
    scripted = ScriptedAnthropic([turn_text("slow", delay_s=0.2)])
    app.dependency_overrides[get_chat_client_factory] = lambda: lambda _key: scripted

    async with _http(app) as http:
        session_id = await create_session(http)
        await http.post(f"/api/sessions/{session_id}/messages", json={"content": "q"})
        # Attach, then walk away mid-turn — the client simply stops reading.
        watcher = asyncio.create_task(http.get(f"/api/sessions/{session_id}/stream"))
        await asyncio.sleep(0.05)
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)

        assert app.state.turn_registry.is_running(session_id)
        await asyncio.gather(app.state.turn_registry.get(session_id).task, return_exceptions=True)
        detail = (await http.get(f"/api/sessions/{session_id}")).json()
        assert detail["messages"][-1]["kind"] == "assistant"


async def test_a_quiet_poll_does_not_end_the_stream(app, with_key, monkeypatch):
    """The disconnect check runs between events and must leave the read alone.

    A turn is mostly silence — a thinking pause is many polls long. A check that
    cancelled the pending read would finish the subscription's generator with it,
    and the stream would stop at the first quiet moment instead of at ``done``.
    """
    monkeypatch.setattr(streaming, "DISCONNECT_POLL_S", 0.01)
    scripted = ScriptedAnthropic([turn_text("answer", delay_s=0.05)])
    app.dependency_overrides[get_chat_client_factory] = lambda: lambda _key: scripted

    async with _http(app) as http:
        session_id = await create_session(http)
        await http.post(f"/api/sessions/{session_id}/messages", json={"content": "q"})
        response = await http.get(f"/api/sessions/{session_id}/stream")

        names = event_names(response.text)
        assert names[-1] == "done"
        assert "text_delta" in names


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
    """The error turn is three events long and is over before the page can attach.

    It used to be unreachable: the registry had already forgotten the turn, so
    ``/stream`` answered 204 and the user saw their question and no notice at all
    — the very thing "persist the question first" exists to prevent. A turn stays
    replayable for ``RECENT_TURN_S`` after it ends, so the error still arrives.
    """
    async with _http(app) as http:
        session_id = await create_session(http)
        accepted = await http.post(f"/api/sessions/{session_id}/messages", json={"content": "q"})
        assert accepted.status_code == 202
        await finish_turn(app, session_id)

        stream = await http.get(f"/api/sessions/{session_id}/stream")
        assert stream.status_code == 200
        assert event_names(stream.text) == ["turn_started", "error", "done"]
        assert payloads_for(stream.text, "error")[0]["message"] == "no API key configured"
        detail = (await http.get(f"/api/sessions/{session_id}")).json()
        assert detail["messages"][0]["kind"] == "user"


async def test_a_turn_that_ended_long_ago_is_a_204_again(app, with_key, monkeypatch):
    """The replay window is short on purpose: the transcript is the record."""
    monkeypatch.setattr(turns, "RECENT_TURN_S", -1.0)
    scripted = ScriptedAnthropic([turn_text("answer")])
    app.dependency_overrides[get_chat_client_factory] = lambda: lambda _key: scripted

    async with _http(app) as http:
        session_id = await create_session(http)
        await http.post(f"/api/sessions/{session_id}/messages", json={"content": "q"})
        await finish_turn(app, session_id)

        assert (await http.get(f"/api/sessions/{session_id}/stream")).status_code == 204


class StubRequest:
    """Just enough of ``Request`` for ``stream_turn_log``: the disconnect flag.

    A real detached client never gets this far in the test suite — an
    ``ASGITransport`` request that is dropped is cancelled, not disconnected — so
    the branch that actually runs against a browser needs a stand-in to reach.
    """

    def __init__(self) -> None:
        self.gone = False

    async def is_disconnected(self) -> bool:
        return self.gone


async def test_a_disconnected_subscriber_leaves_and_the_turn_runs_on(monkeypatch):
    """The poll exists to notice a browser that went away — and to do no more.

    Ending this subscriber must not close the log, must not stop the turn, and
    must not leave the pending read behind as an orphan task.
    """
    monkeypatch.setattr(streaming, "DISCONNECT_POLL_S", 0.01)
    log = TurnLog()
    log.append(ev.TextDelta(text="one"))
    request = StubRequest()

    stream = streaming.stream_turn_log(request, log)
    assert (await stream.__anext__())["event"] == "text_delta"

    running_before = asyncio.all_tasks()
    request.gone = True
    with pytest.raises(StopAsyncIteration):
        # Bounded: with the disconnect check gone this waits for an event that
        # will never come, which is the bug the branch prevents.
        await asyncio.wait_for(stream.__anext__(), 2.0)

    assert log.closed is False
    log.append(ev.TextDelta(text="two"))  # the turn is still writing to it
    assert asyncio.all_tasks() - running_before == set()
