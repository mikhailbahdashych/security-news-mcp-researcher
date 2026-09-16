"""Session CRUD and the SSE chat endpoint.

Every test runs against a scripted Anthropic client — the chat client factory is
overridden app-wide, so no test can reach the network even by accident.
"""

from __future__ import annotations

import asyncio
import logging

import httpx2
import pytest
from fakes.anthropic import ScriptedAnthropic, turn_text, turn_tool_use
from httpx2 import ASGITransport
from sqlalchemy import func, select
from sse_util import parse_sse, payloads_for

from app.agent import persistence
from app.api import tasks as task_registry
from app.api.deps import get_chat_client_factory
from app.db.models import Feed, FeedItem, Message, Note, ResearchSession, ToolCall, utcnow
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


async def test_sessions_list_searches_message_previews(client, session_factory):
    """The sidebar filter has to find a chat by what was said in it.

    A title is the first sixty characters of the first question; everything asked
    afterwards would be unfindable if only titles were searched.
    """
    match = await create_session(client)
    other = await create_session(client)
    async with session_factory() as session:
        session.add(
            Message(
                session_id=match,
                seq=1,
                role="user",
                kind="user",
                content_json=[],
                text_preview="does CVE-2026-12345 affect us?",
            )
        )
        session.add(
            Message(
                session_id=other,
                seq=1,
                role="user",
                kind="user",
                content_json=[],
                text_preview="unrelated chatter",
            )
        )
        await session.commit()

    found = await client.get("/api/sessions", params={"q": "cve-2026-12345"})

    assert [row["id"] for row in found.json()["sessions"]] == [match]


async def test_sessions_list_search_escapes_like_wildcards(client):
    await client.post("/api/sessions", json={"title": "rollout 100% done"})
    await client.post("/api/sessions", json={"title": "1000 hosts affected"})

    found = await client.get("/api/sessions", params={"q": "100%"})

    assert [row["title"] for row in found.json()["sessions"]] == ["rollout 100% done"]


async def test_a_session_matching_many_messages_appears_once(client, session_factory):
    session_id = await create_session(client)
    async with session_factory() as session:
        for seq in range(1, 4):
            session.add(
                Message(
                    session_id=session_id,
                    seq=seq,
                    role="user",
                    kind="user",
                    content_json=[],
                    text_preview=f"message {seq} about CVE-2026-12345",
                )
            )
        await session.commit()

    found = await client.get("/api/sessions", params={"q": "CVE-2026-12345"})

    assert [row["id"] for row in found.json()["sessions"]] == [session_id]


# --------------------------------------------------------------- archived filter


async def test_archived_filter_has_three_states(client):
    live = await create_session(client)
    archived = await create_session(client)
    await client.patch(f"/api/sessions/{archived}", json={"archived": True})

    async def ids(**params) -> list[int]:
        response = await client.get("/api/sessions", params=params)
        assert response.status_code == 200
        return sorted(row["id"] for row in response.json()["sessions"])

    assert await ids() == [live]
    assert await ids(archived="false") == [live]
    assert await ids(archived="true") == [archived]
    assert await ids(archived="all") == sorted([live, archived])


async def test_an_unknown_archived_value_is_a_422(client):
    assert (await client.get("/api/sessions", params={"archived": "nope"})).status_code == 422


# ------------------------------------------------------------------ pagination


async def test_keyset_pagination_walks_every_session_exactly_once(client):
    created = [await create_session(client) for _ in range(7)]

    seen: list[int] = []
    cursor: str | None = None
    for _ in range(10):  # a guard against a cursor that never terminates
        params = {"limit": 3}
        if cursor:
            params["cursor"] = cursor
        page = (await client.get("/api/sessions", params=params)).json()
        seen.extend(row["id"] for row in page["sessions"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert cursor is None
    assert sorted(seen) == sorted(created)
    assert len(seen) == len(set(seen))


async def test_a_malformed_cursor_is_a_422(client):
    assert (await client.get("/api/sessions", params={"cursor": "!!"})).status_code == 422


# ---------------------------------------------------------------------- PATCH


async def test_patch_renames_and_bumps_updated_at(client):
    session_id = await create_session(client)
    before = (await client.get(f"/api/sessions/{session_id}")).json()["session"]["updated_at"]

    renamed = await client.patch(f"/api/sessions/{session_id}", json={"title": "  Renamed  "})

    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Renamed"
    assert renamed.json()["updated_at"] > before


async def test_a_patch_that_changes_nothing_leaves_the_ordering_alone(client):
    """The sidebar is ordered by ``updated_at``, so a no-op PATCH that bumped it
    reordered the user's history for nothing — a rename dialog opened and
    confirmed unchanged used to float the thread to the top."""
    session_id = await create_session(client)
    stamp = (
        await client.patch(f"/api/sessions/{session_id}", json={"title": "Same"})
    ).json()["updated_at"]

    again = await client.patch(
        f"/api/sessions/{session_id}", json={"title": "  Same  ", "archived": False}
    )
    empty = await client.patch(f"/api/sessions/{session_id}", json={})

    assert again.json()["title"] == "Same"
    assert again.json()["updated_at"] == stamp
    assert empty.json()["updated_at"] == stamp


async def test_archiving_still_bumps_updated_at(client):
    session_id = await create_session(client)
    before = (await client.get(f"/api/sessions/{session_id}")).json()["session"]["updated_at"]

    archived = await client.patch(f"/api/sessions/{session_id}", json={"archived": True})

    assert archived.json()["archived"] is True
    assert archived.json()["updated_at"] > before


async def test_patch_with_an_empty_title_clears_it(client):
    session_id = await create_session(client)
    await client.patch(f"/api/sessions/{session_id}", json={"title": "Named"})

    cleared = await client.patch(f"/api/sessions/{session_id}", json={"title": ""})

    assert cleared.json()["title"] is None


async def test_archiving_round_trips(client):
    session_id = await create_session(client)

    assert (
        await client.patch(f"/api/sessions/{session_id}", json={"archived": True})
    ).json()["archived"] is True
    assert (
        await client.patch(f"/api/sessions/{session_id}", json={"archived": False})
    ).json()["archived"] is False
    assert [row["id"] for row in (await client.get("/api/sessions")).json()["sessions"]] == [
        session_id
    ]


async def test_patching_an_unknown_session_is_a_404(client):
    assert (await client.patch("/api/sessions/999", json={"title": "x"})).status_code == 404


async def test_deleting_an_unknown_session_is_a_404(client):
    assert (await client.delete("/api/sessions/999")).status_code == 404


# ------------------------------------------------------------- delete cascade


async def test_the_test_engine_enforces_foreign_keys(db_engine):
    """Without this pragma the cascade tests below would pass for the wrong reason.

    SQLite ignores ``ON DELETE`` clauses unless ``foreign_keys`` is on, so a
    cascade test on an engine without it asserts nothing at all.
    """
    async with db_engine.connect() as connection:
        result = await connection.exec_driver_sql("PRAGMA foreign_keys")
        assert result.scalar() == 1


async def test_delete_keeps_the_notes_the_session_produced(client, session_factory):
    """The load-bearing cascade: messages and tool calls go, notes stay.

    A note outlives the chat that produced it — ``notes.session_id`` is
    ON DELETE SET NULL — so deleting a session must not take the week's write-up
    with it. Asserted against the tables directly rather than through the API,
    because an ORM-level cascade could satisfy the API and still leave orphans.
    """
    session_id = await create_session(client)
    async with session_factory() as session:
        for seq in (1, 2):
            message = Message(
                session_id=session_id,
                seq=seq,
                role="assistant",
                kind="assistant",
                content_json=[],
            )
            session.add(message)
            await session.flush()
            session.add(
                ToolCall(
                    message_id=message.id,
                    tool_use_id=f"toolu_{seq}",
                    name="search_feed_items",
                    source="builtin",
                )
            )
        session.add(
            Note(title="Weekly notes", body_md="# Weekly\n\nthe body", session_id=session_id)
        )
        await session.commit()

    assert (await client.delete(f"/api/sessions/{session_id}")).status_code == 204

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 0
        assert await session.scalar(select(func.count()).select_from(ToolCall)) == 0
        note = await session.scalar(select(Note))
        assert note is not None
        assert note.session_id is None
        assert note.body_md == "# Weekly\n\nthe body"


# ------------------------------------------------------------------ auto-title


async def test_the_first_user_message_titles_an_untitled_session(client, session_factory):
    session_id = await create_session(client)

    await persistence.append_user_message(
        session_factory, session_id, [{"type": "text", "text": "  what is\n CVE-2026-12345?  "}]
    )

    title = (await client.get(f"/api/sessions/{session_id}")).json()["session"]["title"]
    assert title == "what is CVE-2026-12345?"


async def test_a_long_first_message_is_cut_on_a_word_boundary(client, session_factory):
    session_id = await create_session(client)
    long_text = "the quick brown fox jumps over the lazy dog and keeps on running for miles"

    await persistence.append_user_message(
        session_factory, session_id, [{"type": "text", "text": long_text}]
    )

    title = (await client.get(f"/api/sessions/{session_id}")).json()["session"]["title"]
    assert title.endswith("\u2026")
    assert len(title) <= persistence.TITLE_CHARS + 1
    assert long_text.startswith(title[:-1])
    assert not title[:-1].endswith(" ")


async def test_a_title_set_by_hand_is_never_overwritten(client, session_factory):
    session_id = await create_session(client)
    await client.patch(f"/api/sessions/{session_id}", json={"title": "Mine"})

    await persistence.append_user_message(
        session_factory, session_id, [{"type": "text", "text": "something else entirely"}]
    )

    title = (await client.get(f"/api/sessions/{session_id}")).json()["session"]["title"]
    assert title == "Mine"


async def test_a_later_message_does_not_retitle_a_titled_session(client, session_factory):
    session_id = await create_session(client)
    await persistence.append_user_message(
        session_factory, session_id, [{"type": "text", "text": "first question"}]
    )

    await persistence.append_user_message(
        session_factory, session_id, [{"type": "text", "text": "second question"}]
    )

    title = (await client.get(f"/api/sessions/{session_id}")).json()["session"]["title"]
    assert title == "first question"


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


async def test_no_api_key_still_keeps_the_question(app, client):
    """The question survives the error, in the transcript.

    Nothing else was persisted on this path, so the user typed a question, saw
    "no API key configured", and watched their own message disappear on the next
    refetch — with nothing to copy out and re-send once the key was set.
    """
    session_id = await create_session(client)

    response = await client.post(
        f"/api/sessions/{session_id}/messages", json={"content": "what is CVE-2026-1234?"}
    )

    done = parse_sse(response.text)[-1]
    detail = (await client.get(f"/api/sessions/{session_id}")).json()
    assert [message["kind"] for message in detail["messages"]] == ["user"]
    assert detail["messages"][0]["content_json"] == [
        {"type": "text", "text": "what is CVE-2026-1234?"}
    ]
    # The id is announced, so the live view can reconcile instead of duplicating.
    assert done[1]["message_ids"] == [detail["messages"][0]["id"]]
    # And the session is titled from it, exactly as a successful turn would.
    assert detail["session"]["title"] == "what is CVE-2026-1234?"


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
        # Which error: `cancelled` is a closed-set type the UI renders as "Stopped",
        # and asserting only that *an* error arrived would pass on an api_error
        # raised by the teardown itself.
        assert [payload["type"] for payload in payloads_for(response.text, "error")] == [
            "cancelled"
        ]

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


async def test_delete_during_a_running_turn_cancels_and_awaits_it_first(
    app, with_key, session_factory, caplog
):
    """DELETE must not return while the turn is still winding down.

    Deleting first and firing a bare ``cancel()`` left the runner free to finish
    a write it had already started — an INSERT for a session that no longer
    exists, i.e. a foreign-key failure swallowed inside the pump task. What the
    fix guarantees is that the task is *done* by the time the response comes
    back, so the scripted stream below takes a measurable moment to unwind and a
    fire-and-forget cancel fails the assertion.
    """
    scripted = ScriptedAnthropic(
        [
            turn_tool_use(
                [("search_feed_items", {"q": "x"})],
                text="looking",
                delay_s=0.02,
                teardown_s=0.25,
            ),
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
        key = task_registry.session_key(session_id)
        assert await task_registry.is_running(key) is True

        with caplog.at_level(logging.ERROR):
            deleted = await http.delete(f"/api/sessions/{session_id}")

        assert deleted.status_code == 204
        # The load-bearing assertion: finished, not merely asked to stop.
        assert await task_registry.is_running(key) is False
        assert "FOREIGN KEY" not in caplog.text
        assert "IntegrityError" not in caplog.text

        await stream

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 0
        assert await session.scalar(select(func.count()).select_from(ResearchSession)) == 0


async def test_delete_does_not_hang_on_a_task_that_ignores_cancellation(monkeypatch):
    """Finding 7: `cancel_and_wait` must be bounded.

    A task can refuse to die — a shielded write, a handler that swallows
    CancelledError — and an unbounded await would park the DELETE behind it
    forever. The wait is capped; the delete goes ahead regardless.
    """
    monkeypatch.setattr(task_registry, "CANCEL_WAIT_S", 0.05)
    await task_registry.clear()

    started = asyncio.Event()
    refused = asyncio.Event()

    async def ignores_cancellation() -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            refused.set()
            # Deliberately does not re-raise, and keeps working.
            await asyncio.sleep(30)

    task = asyncio.create_task(ignores_cancellation())
    await started.wait()
    await task_registry.register("session:99", task)

    began = asyncio.get_running_loop().time()
    cancelled = await task_registry.cancel_and_wait("session:99")
    elapsed = asyncio.get_running_loop().time() - began

    assert cancelled is True
    assert refused.is_set() is True
    assert task.done() is False  # it really did ignore us
    assert elapsed < 1.0  # ...and we did not wait for it

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await task_registry.clear()


async def test_delete_still_returns_204_when_the_turn_will_not_stop(
    app, with_key, session_factory, monkeypatch
):
    monkeypatch.setattr(task_registry, "CANCEL_WAIT_S", 0.05)
    scripted = ScriptedAnthropic(
        [
            turn_tool_use(
                [("search_feed_items", {"q": "x"})],
                text="looking",
                delay_s=0.02,
                # Far longer than the capped wait: the delete must not block on it.
                teardown_s=0.8,
            ),
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

        began = asyncio.get_running_loop().time()
        deleted = await http.delete(f"/api/sessions/{session_id}")
        elapsed = asyncio.get_running_loop().time() - began

        assert deleted.status_code == 204
        assert elapsed < 1.0
        await stream

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ResearchSession)) == 0
