"""Session CRUD and the SSE chat endpoint.

Every test runs against a scripted Anthropic client — the chat client factory is
overridden app-wide, so no test can reach the network even by accident.
"""

from __future__ import annotations

import asyncio
import json

import httpx2
import pytest
from fakes.anthropic import ScriptedAnthropic, turn_text, turn_tool_use
from httpx2 import ASGITransport
from sqlalchemy import func, select

from app.api import tasks as task_registry
from app.api.deps import get_chat_client_factory
from app.db.models import Feed, FeedItem, Message, ResearchSession, ToolCall, utcnow
from app.services import settings as settings_service


@pytest.fixture(autouse=True)
async def clean_task_registry():
    await task_registry.clear()
    yield
    await task_registry.clear()


@pytest.fixture
async def with_key(db_session):
    await settings_service.set_value(db_session, "anthropic_api_key", "sk-ant-test")
    await db_session.commit()


def use_script(app, *turns) -> ScriptedAnthropic:
    """Point the chat endpoint at a scripted client and hand it back."""
    client = ScriptedAnthropic(list(turns))
    app.dependency_overrides[get_chat_client_factory] = lambda: lambda _key: client
    return client


def parse_sse(body: str) -> list[tuple[str, dict]]:
    """Parse a raw SSE body into ``(event, payload)`` pairs, ignoring heartbeats."""
    parsed: list[tuple[str, dict]] = []
    for frame in body.replace("\r\n", "\n").split("\n\n"):
        name = "message"
        data: list[str] = []
        for line in frame.split("\n"):
            if not line or line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                name = value
            elif field == "data":
                data.append(value)
        if data:
            parsed.append((name, json.loads("\n".join(data))))
    return parsed


async def create_session(client: httpx2.AsyncClient) -> int:
    response = await client.post("/api/sessions", json={})
    assert response.status_code == 201
    return response.json()["id"]


# ------------------------------------------------------------------- CRUD


async def test_session_crud(client):
    created = await client.post("/api/sessions", json={"title": "Log4Shell redux"})
    assert created.status_code == 201
    body = created.json()
    assert body["title"] == "Log4Shell redux"
    assert body["model"] == "claude-opus-5"  # pinned from settings at creation
    session_id = body["id"]

    listed = await client.get("/api/sessions")
    assert [row["id"] for row in listed.json()["sessions"]] == [session_id]

    detail = await client.get(f"/api/sessions/{session_id}")
    assert detail.json()["messages"] == []

    patched = await client.patch(f"/api/sessions/{session_id}", json={"title": "Renamed"})
    assert patched.json()["title"] == "Renamed"

    archived = await client.patch(f"/api/sessions/{session_id}", json={"archived": True})
    assert archived.json()["archived"] is True
    assert (await client.get("/api/sessions")).json()["sessions"] == []
    assert len((await client.get("/api/sessions?archived=true")).json()["sessions"]) == 1

    assert (await client.delete(f"/api/sessions/{session_id}")).status_code == 204
    assert (await client.get(f"/api/sessions/{session_id}")).status_code == 404


async def test_delete_cascades_messages_and_tool_calls(client, session_factory, db_session):
    session_id = await create_session(client)
    async with session_factory() as session:
        message = Message(
            session_id=session_id, seq=1, role="assistant", kind="assistant", content_json=[]
        )
        session.add(message)
        await session.flush()
        session.add(
            ToolCall(message_id=message.id, tool_use_id="toolu_0", name="x", source="builtin")
        )
        await session.commit()

    await client.delete(f"/api/sessions/{session_id}")

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 0
        assert await session.scalar(select(func.count()).select_from(ToolCall)) == 0


async def test_sessions_list_searches_titles(client):
    await client.post("/api/sessions", json={"title": "AcmeVPN advisory"})
    await client.post("/api/sessions", json={"title": "Phishing wave"})

    found = await client.get("/api/sessions?q=acmevpn")

    assert [row["title"] for row in found.json()["sessions"]] == ["AcmeVPN advisory"]


# ------------------------------------------------------------------- SSE


async def test_stream_headers_and_event_sequence(app, client, with_key):
    use_script(
        app,
        turn_tool_use([("search_feed_items", {"q": "CVE-2026-1234"})]),
        turn_text("Nothing in the inbox matches."),
    )
    session_id = await create_session(client)

    response = await client.post(
        f"/api/sessions/{session_id}/messages", json={"content": "what happened?"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"].startswith("no-cache")

    events = [name for name, _ in parse_sse(response.text)]
    assert events[0] == "turn_start"
    assert events[-1] == "done"
    assert "tool_use_start" in events
    assert "tool_use_input" in events
    assert events.index("tool_use_start") < events.index("turn_end")
    assert events.index("turn_end") < events.index("tool_result")
    assert events.count("turn_start") == 2
    assert "text_delta" in events


async def test_stream_persists_the_transcript(app, client, with_key, session_factory):
    use_script(
        app,
        turn_tool_use([("search_feed_items", {"q": "x"})]),
        turn_text("Here is what I found."),
    )
    session_id = await create_session(client)

    await client.post(f"/api/sessions/{session_id}/messages", json={"content": "hello"})

    detail = (await client.get(f"/api/sessions/{session_id}")).json()
    assert [message["kind"] for message in detail["messages"]] == [
        "user",
        "assistant",
        "tool_result",
        "assistant",
    ]
    assistant = detail["messages"][1]
    assert assistant["tool_calls"][0]["name"] == "search_feed_items"
    assert assistant["tool_calls"][0]["source"] == "builtin"
    assert detail["session"]["title"] == "hello"
    assert detail["session"]["total_output_tokens"] > 0


async def test_no_api_key_yields_an_error_event(app, client):
    session_id = await create_session(client)

    response = await client.post(f"/api/sessions/{session_id}/messages", json={"content": "hello"})

    events = parse_sse(response.text)
    assert [name for name, _ in events] == ["error", "done"]
    assert events[0][1]["type"] == "api_error"
    assert events[0][1]["message"] == "no API key configured"


async def test_attached_item_ids_are_resolved_into_the_persisted_content(
    app, client, with_key, db_session
):
    feed = Feed(url="https://example.test/rss", title="Example")
    db_session.add(feed)
    await db_session.flush()
    item = FeedItem(
        feed_id=feed.id,
        guid="g1",
        url="https://example.test/a",
        title="AcmeVPN RCE",
        summary="Pre-auth RCE.",
        published_at=utcnow(),
    )
    db_session.add(item)
    await db_session.commit()

    use_script(app, turn_text("ok"))
    session_id = await create_session(client)

    await client.post(
        f"/api/sessions/{session_id}/messages",
        json={"content": "summarise these", "attached_item_ids": [item.id]},
    )

    detail = (await client.get(f"/api/sessions/{session_id}")).json()
    blocks = detail["messages"][0]["content_json"]
    assert blocks[0] == {"type": "text", "text": "summarise these"}
    assert "Attached feed items:" in blocks[1]["text"]
    assert f"id {item.id} · AcmeVPN RCE" in blocks[1]["text"]


async def test_server_tools_are_absent_when_both_toggles_are_off(app, client, with_key, db_session):
    await settings_service.set_many(
        db_session, {"web_search_enabled": "false", "web_fetch_enabled": "false"}
    )
    await db_session.commit()
    scripted = use_script(app, turn_text("only local sources here"))
    session_id = await create_session(client)

    await client.post(f"/api/sessions/{session_id}/messages", json={"content": "hi"})

    names = [definition["name"] for definition in scripted.calls[0]["tools"]]
    assert names == ["fetch_article", "get_feed_item", "search_feed_items"]


async def test_a_second_concurrent_turn_is_a_conflict(app, with_key, session_factory):
    """A slow scripted stream keeps the first turn open while the second POSTs."""
    scripted = ScriptedAnthropic([turn_text("slow answer", delay_s=0.05), turn_text("second")])
    app.dependency_overrides[get_chat_client_factory] = lambda: lambda _key: scripted

    async with httpx2.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        session_id = await create_session(http)

        first = asyncio.create_task(
            http.post(f"/api/sessions/{session_id}/messages", json={"content": "one"})
        )
        await asyncio.sleep(0.05)
        second = await http.post(f"/api/sessions/{session_id}/messages", json={"content": "two"})
        assert second.status_code == 409
        await first


async def test_cancel_stops_a_running_turn_and_keeps_what_was_persisted(
    app, with_key, session_factory
):
    scripted = ScriptedAnthropic(
        [
            # Slow enough that the cancel lands mid-turn.
            turn_tool_use([("search_feed_items", {"q": "x"})], text="looking", delay_s=0.05),
            turn_text("done"),
        ]
    )
    app.dependency_overrides[get_chat_client_factory] = lambda: lambda _key: scripted

    async with httpx2.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        session_id = await create_session(http)
        stream = asyncio.create_task(
            http.post(f"/api/sessions/{session_id}/messages", json={"content": "go"})
        )
        await asyncio.sleep(0.08)
        cancelled = await http.post(f"/api/sessions/{session_id}/cancel")
        assert cancelled.json() == {"cancelled": True}

        response = await stream
        events = [name for name, _ in parse_sse(response.text)]
        assert events[-1] == "done"
        assert "error" in events

        detail = (await http.get(f"/api/sessions/{session_id}")).json()
        # The user message committed before the first LLM call, so it survives.
        assert detail["messages"][0]["kind"] == "user"


async def test_cancel_with_nothing_running_is_a_200(client):
    session_id = await create_session(client)

    response = await client.post(f"/api/sessions/{session_id}/cancel")

    assert response.status_code == 200
    assert response.json() == {"cancelled": False}


async def test_cancel_for_an_unknown_session_is_a_404(client):
    assert (await client.post("/api/sessions/999/cancel")).status_code == 404


async def test_posting_to_an_unknown_session_is_a_404(client, with_key):
    response = await client.post("/api/sessions/999/messages", json={"content": "x"})
    assert response.status_code == 404


async def test_mid_turn_refresh_sees_a_partial_transcript(app, client, with_key, session_factory):
    """A GET during a turn must return whatever has been committed so far."""
    use_script(
        app,
        turn_tool_use([("search_feed_items", {"q": "x"})]),
        turn_text("finished"),
    )
    session_id = await create_session(client)
    await client.post(f"/api/sessions/{session_id}/messages", json={"content": "go"})

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(Message.kind)
                    .where(Message.session_id == session_id)
                    .order_by(Message.seq)
                )
            )
            .scalars()
            .all()
        )
    # Each of these committed in its own transaction as the turn progressed.
    assert rows == ["user", "assistant", "tool_result", "assistant"]


async def test_no_session_row_is_written_for_an_unknown_session(client, session_factory):
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ResearchSession)) == 0
