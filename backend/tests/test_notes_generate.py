"""Note generation: context assembly, the restricted tool set, and what is saved.

Every test drives a scripted Anthropic client through the real streaming
endpoint, so the assertions are about the request that *would* have gone to the
API and the rows that came out of it. Nothing reaches the network.
"""

from __future__ import annotations

import asyncio
import json

import httpx2
import pytest
from fakes.anthropic import (
    ScriptedAnthropic,
    turn_refusal,
    turn_text,
    turn_tool_use,
    turn_web_search,
)
from httpx2 import ASGITransport
from sqlalchemy import func, select
from sse_util import event_names, parse_sse, payloads_for

from app.api import tasks as task_registry
from app.api.deps import get_chat_client_factory
from app.db.models import Feed, FeedItem, Message, Note, NoteSource, ResearchSession, utcnow
from app.services import extract as extract_service
from app.services import notes as notes_service
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
    client = ScriptedAnthropic(list(turns))
    app.dependency_overrides[get_chat_client_factory] = lambda: lambda _key: client
    return client


async def seed_items(
    session_factory, count: int = 2, *, content: str | None = "body text"
) -> list[int]:
    async with session_factory() as session:
        feed = Feed(url="https://example.test/rss", title="Example Security")
        session.add(feed)
        await session.flush()
        ids = []
        for index in range(count):
            item = FeedItem(
                feed_id=feed.id,
                guid=f"g{index}",
                url=f"https://example.test/story-{index}",
                title=f"Story {index}: AcmeVPN pre-auth RCE",
                summary=f"Summary for story {index}.",
                content_text=None if content is None else f"{content} {index}",
                published_at=utcnow(),
            )
            session.add(item)
            await session.flush()
            ids.append(item.id)
        await session.commit()
        return ids


async def generate(client: httpx2.AsyncClient, **body) -> httpx2.Response:
    return await client.post("/api/notes/generate", json=body)


async def note_rows(session_factory) -> list[Note]:
    async with session_factory() as session:
        return list((await session.execute(select(Note).order_by(Note.id))).scalars().all())


async def source_rows(session_factory) -> list[NoteSource]:
    async with session_factory() as session:
        return list(
            (await session.execute(select(NoteSource).order_by(NoteSource.id))).scalars().all()
        )


def system_prompt(scripted: ScriptedAnthropic) -> str:
    return scripted.calls[0]["system"]


def user_text(scripted: ScriptedAnthropic) -> str:
    blocks = scripted.calls[0]["messages"][0]["content"]
    return "\n".join(block["text"] for block in blocks if block.get("type") == "text")


# ------------------------------------------------------------- happy path


async def test_items_only_generation_saves_the_note_and_its_sources(
    app, client, with_key, session_factory
):
    item_ids = await seed_items(session_factory, 2)
    use_script(app, turn_text("## Story 0\n**What happened** — it happened."))

    response = await generate(client, item_ids=item_ids, title="Weekly security notes")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"].startswith("no-cache")

    names = event_names(response.text)
    assert names[0] == "turn_start"
    assert names[-1] == "done"
    assert "text_delta" in names

    notes = await note_rows(session_factory)
    assert len(notes) == 1
    assert notes[0].title == "Weekly security notes"
    assert notes[0].body_md == "## Story 0\n**What happened** — it happened."
    assert notes[0].session_id is None
    assert notes[0].template_used == settings_service.DEFAULT_NOTE_TEMPLATE

    sources = await source_rows(session_factory)
    assert [source.feed_item_id for source in sources] == item_ids
    assert [source.url for source in sources] == [
        "https://example.test/story-0",
        "https://example.test/story-1",
    ]

    done = payloads_for(response.text, "done")[-1]
    assert done == {"note_id": notes[0].id}


async def test_turn_start_carries_the_generation_id(app, client, with_key, session_factory):
    item_ids = await seed_items(session_factory, 1)
    use_script(app, turn_text("notes"))

    response = await generate(client, item_ids=item_ids, generation_id="gen-123")

    assert payloads_for(response.text, "turn_start")[0]["generation_id"] == "gen-123"


async def test_a_server_generated_id_is_announced_on_turn_start(
    app, client, with_key, session_factory
):
    item_ids = await seed_items(session_factory, 1)
    use_script(app, turn_text("notes"))

    response = await generate(client, item_ids=item_ids)

    announced = payloads_for(response.text, "turn_start")[0]["generation_id"]
    assert isinstance(announced, str) and announced


async def test_generation_writes_no_session_or_message_rows(
    app, client, with_key, session_factory
):
    item_ids = await seed_items(session_factory, 1)
    use_script(app, turn_text("notes"))

    await generate(client, item_ids=item_ids)

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 0
        assert await session.scalar(select(func.count()).select_from(ResearchSession)) == 0


# ------------------------------------------------------------- validation


async def test_a_body_with_neither_items_nor_a_session_is_a_422(client):
    assert (await generate(client)).status_code == 422
    assert (await generate(client, item_ids=[])).status_code == 422


async def test_more_than_the_item_cap_is_a_422(client):
    ids = list(range(1, notes_service.MAX_NOTE_ITEMS + 2))

    assert (await generate(client, item_ids=ids)).status_code == 422


async def test_unknown_item_and_session_ids_are_404s_and_save_nothing(
    app, client, with_key, session_factory
):
    use_script(app, turn_text("never reached"))

    assert (await generate(client, item_ids=[4242])).status_code == 404
    assert (await generate(client, session_id=4242)).status_code == 404
    assert await note_rows(session_factory) == []


# --------------------------------------------------------------- template


async def test_template_override_beats_the_settings_template(
    app, client, with_key, session_factory
):
    item_ids = await seed_items(session_factory, 1)
    scripted = use_script(app, turn_text("notes"))
    override = "## {Item title}\n**Detection ideas** — …"

    await generate(client, item_ids=item_ids, template_override=override)

    prompt = system_prompt(scripted)
    assert override in prompt
    assert settings_service.DEFAULT_NOTE_TEMPLATE not in prompt
    assert (await note_rows(session_factory))[0].template_used == override


async def test_blank_override_falls_back_to_the_settings_template(
    app, client, with_key, session_factory, db_session
):
    await settings_service.set_value(db_session, "note_template", "## {Item title}\n**Only** — …")
    await db_session.commit()
    item_ids = await seed_items(session_factory, 1)
    scripted = use_script(app, turn_text("notes"))

    await generate(client, item_ids=item_ids, template_override="   ")

    assert "**Only** — …" in system_prompt(scripted)
    assert (await note_rows(session_factory))[0].template_used == "## {Item title}\n**Only** — …"


async def test_system_prompt_extra_is_appended(app, client, with_key, session_factory, db_session):
    await settings_service.set_value(db_session, "system_prompt_extra", "Always mention KEVs.")
    await db_session.commit()
    item_ids = await seed_items(session_factory, 1)
    scripted = use_script(app, turn_text("notes"))

    await generate(client, item_ids=item_ids)

    prompt = system_prompt(scripted)
    assert prompt.endswith("Always mention KEVs.")
    assert settings_service.DEFAULT_NOTE_TEMPLATE in prompt


# ---------------------------------------------------------------- context


async def test_item_context_carries_title_url_feed_and_text(
    app, client, with_key, session_factory
):
    item_ids = await seed_items(session_factory, 1)
    scripted = use_script(app, turn_text("notes"))

    await generate(client, item_ids=item_ids)

    text = user_text(scripted)
    assert "Story 0: AcmeVPN pre-auth RCE" in text
    assert "https://example.test/story-0" in text
    assert "Example Security" in text
    assert "body text 0" in text


async def test_long_item_text_is_truncated(app, client, with_key, session_factory):
    async with session_factory() as session:
        feed = Feed(url="https://example.test/rss", title="Example")
        session.add(feed)
        await session.flush()
        item = FeedItem(
            feed_id=feed.id,
            guid="long",
            url="https://example.test/long",
            title="Long one",
            content_text="x" * (notes_service.NOTE_ITEM_MAX_CHARS + 500),
            published_at=utcnow(),
        )
        session.add(item)
        await session.commit()
        item_id = item.id

    scripted = use_script(app, turn_text("notes"))
    await generate(client, item_ids=[item_id])

    text = user_text(scripted)
    assert "[truncated]" in text
    assert "x" * (notes_service.NOTE_ITEM_MAX_CHARS + 1) not in text


async def test_session_transcript_is_text_only(app, client, with_key, session_factory):
    async with session_factory() as session:
        research = ResearchSession(title="Log4Shell", model="claude-opus-5")
        session.add(research)
        await session.flush()
        session.add_all(
            [
                Message(
                    session_id=research.id,
                    seq=1,
                    role="user",
                    kind="user",
                    content_json=[{"type": "text", "text": "what happened with acmevpn"}],
                ),
                Message(
                    session_id=research.id,
                    seq=2,
                    role="assistant",
                    kind="assistant",
                    content_json=[
                        {
                            "type": "thinking",
                            "thinking": "SECRET-THINKING",
                            "signature": "sig",
                        },
                        {"type": "text", "text": "A pre-auth RCE, patched in 3.2.1."},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "search_feed_items",
                            "input": {"q": "SECRET-TOOL-INPUT"},
                        },
                    ],
                    stop_reason="tool_use",
                ),
                Message(
                    session_id=research.id,
                    seq=3,
                    role="user",
                    kind="tool_result",
                    content_json=[
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "SECRET-TOOL-RESULT",
                        }
                    ],
                ),
                Message(
                    session_id=research.id,
                    seq=4,
                    role="assistant",
                    kind="assistant",
                    content_json=[{"type": "text", "text": "Patch by Friday."}],
                    stop_reason="end_turn",
                ),
            ]
        )
        await session.commit()
        session_id = research.id

    scripted = use_script(app, turn_text("## notes"))
    before = await _count(session_factory, Message)

    await generate(client, session_id=session_id)

    text = user_text(scripted)
    assert "Research session transcript" in text
    assert "what happened with acmevpn" in text
    assert "A pre-auth RCE, patched in 3.2.1." in text
    assert "Patch by Friday." in text
    assert "SECRET-THINKING" not in text
    assert "SECRET-TOOL-INPUT" not in text
    assert "SECRET-TOOL-RESULT" not in text

    saved = await note_rows(session_factory)
    assert saved[0].session_id == session_id
    assert await _count(session_factory, Message) == before
    assert await _count(session_factory, ResearchSession) == 1


async def _count(session_factory, model) -> int:
    async with session_factory() as session:
        return await session.scalar(select(func.count()).select_from(model))


# ------------------------------------------------------------------ title


async def test_explicit_title_wins(app, client, with_key, session_factory):
    item_ids = await seed_items(session_factory, 2)
    use_script(app, turn_text("notes"))

    await generate(client, item_ids=item_ids, title="  Tuesday sync  ")

    assert (await note_rows(session_factory))[0].title == "Tuesday sync"


async def test_a_single_item_lends_its_title(app, client, with_key, session_factory):
    item_ids = await seed_items(session_factory, 1)
    use_script(app, turn_text("notes"))

    await generate(client, item_ids=item_ids)

    assert (await note_rows(session_factory))[0].title == "Story 0: AcmeVPN pre-auth RCE"


async def test_several_items_get_a_dated_title(app, client, with_key, session_factory):
    item_ids = await seed_items(session_factory, 2)
    use_script(app, turn_text("notes"))

    await generate(client, item_ids=item_ids)

    assert (await note_rows(session_factory))[0].title == f"Security notes — {utcnow():%Y-%m-%d}"


# ------------------------------------------------------------- extraction


async def test_an_item_without_text_is_extracted_on_demand(
    app, client, with_key, session_factory, monkeypatch
):
    item_ids = await seed_items(session_factory, 1, content=None)
    calls: list[int] = []

    async def fake_extract(session, item_id, **_kwargs):
        calls.append(item_id)
        item = await session.get(FeedItem, item_id)
        item.content_text = "EXTRACTED ARTICLE BODY"
        return extract_service.ItemExtractResult(item=item, extracted=True, fallback=False)

    monkeypatch.setattr(extract_service, "extract_item", fake_extract)
    scripted = use_script(app, turn_text("notes"))

    await generate(client, item_ids=item_ids)

    assert calls == item_ids
    assert "EXTRACTED ARTICLE BODY" in user_text(scripted)


async def test_thin_extraction_falls_back_to_the_rss_summary(
    app, client, with_key, session_factory, monkeypatch
):
    item_ids = await seed_items(session_factory, 1, content=None)

    async def fake_extract(session, item_id, **_kwargs):
        item = await session.get(FeedItem, item_id)
        return extract_service.ItemExtractResult(
            item=item, extracted=False, fallback=True, reason=extract_service.REASON_THIN
        )

    monkeypatch.setattr(extract_service, "extract_item", fake_extract)
    scripted = use_script(app, turn_text("notes"))

    response = await generate(client, item_ids=item_ids)

    assert response.status_code == 200
    assert "Summary for story 0." in user_text(scripted)
    assert len(await note_rows(session_factory)) == 1


async def test_a_raising_extractor_does_not_fail_the_generation(
    app, client, with_key, session_factory, monkeypatch
):
    item_ids = await seed_items(session_factory, 1, content=None)

    async def boom(_session, _item_id, **_kwargs):
        raise RuntimeError("the extractor fell over")

    monkeypatch.setattr(extract_service, "extract_item", boom)
    scripted = use_script(app, turn_text("notes"))

    response = await generate(client, item_ids=item_ids)

    assert response.status_code == 200
    assert "Story 0: AcmeVPN pre-auth RCE" in user_text(scripted)
    assert len(await note_rows(session_factory)) == 1


# --------------------------------------------------------------- tool set


async def test_the_tool_set_is_restricted(app, client, with_key, session_factory):
    item_ids = await seed_items(session_factory, 1)
    scripted = use_script(app, turn_text("notes"))

    await generate(client, item_ids=item_ids)

    names = [definition["name"] for definition in scripted.calls[0]["tools"]]
    assert names == ["fetch_article", "get_feed_item", "web_search"]
    assert not any(name.startswith("mcp__") for name in names)


async def test_web_search_is_absent_when_the_toggle_is_off(
    app, client, with_key, session_factory, db_session
):
    await settings_service.set_value(db_session, "web_search_enabled", "false")
    await db_session.commit()
    item_ids = await seed_items(session_factory, 1)
    scripted = use_script(app, turn_text("notes"))

    await generate(client, item_ids=item_ids)

    names = [definition["name"] for definition in scripted.calls[0]["tools"]]
    assert names == ["fetch_article", "get_feed_item"]


# ------------------------------------------------------------ tool results


async def test_a_fetched_url_becomes_an_extra_source(
    app, client, with_key, session_factory, monkeypatch
):
    item_ids = await seed_items(session_factory, 1)

    async def fake_article(url, *_args, **_kwargs):
        return extract_service.ExtractResult(ok=True, text="advisory body")

    monkeypatch.setattr(extract_service, "extract_article", fake_article)
    use_script(
        app,
        turn_tool_use([("fetch_article", {"url": "https://vendor.test/advisory"})]),
        turn_text("## Story 0\nwith a [source](https://vendor.test/advisory)"),
    )

    response = await generate(client, item_ids=item_ids)

    assert "tool_result" in event_names(response.text)
    sources = await source_rows(session_factory)
    assert [(source.feed_item_id, source.url) for source in sources] == [
        (item_ids[0], "https://example.test/story-0"),
        (None, "https://vendor.test/advisory"),
    ]


async def test_a_fetched_url_matching_an_item_is_not_duplicated(
    app, client, with_key, session_factory, monkeypatch
):
    item_ids = await seed_items(session_factory, 1)

    async def fake_article(url, *_args, **_kwargs):
        return extract_service.ExtractResult(ok=True, text="the same article")

    monkeypatch.setattr(extract_service, "extract_article", fake_article)
    use_script(
        app,
        turn_tool_use([("fetch_article", {"url": "https://example.test/story-0"})]),
        turn_text("notes"),
    )

    await generate(client, item_ids=item_ids)

    sources = await source_rows(session_factory)
    assert len(sources) == 1
    assert sources[0].feed_item_id == item_ids[0]


async def test_cited_web_search_results_become_sources(app, client, with_key, session_factory):
    """A search result is a source only once the note actually cites it.

    One query hands back ten results, mostly aggregators reprinting the same
    story; storing all of them buries the handful the note was written from.
    """
    item_ids = await seed_items(session_factory, 1)
    use_script(
        app,
        turn_web_search(
            results=[
                ("Vendor advisory", "https://vendor.test/advisory"),
                ("Duplicate", "https://vendor.test/advisory"),
                ("Not a web page", "ftp://vendor.test/file"),
                ("Never cited", "https://aggregator.test/reprint"),
            ],
            text="## Story 0\nSee the [advisory](https://vendor.test/advisory).",
        ),
    )

    await generate(client, item_ids=item_ids)

    sources = await source_rows(session_factory)
    assert [(source.feed_item_id, source.url, source.title) for source in sources] == [
        (item_ids[0], "https://example.test/story-0", "Story 0: AcmeVPN pre-auth RCE"),
        (None, "https://vendor.test/advisory", "Vendor advisory"),
    ]


async def test_a_fetched_url_is_kept_even_when_the_note_does_not_link_it(
    app, client, with_key, session_factory, monkeypatch
):
    """Unlike a search hit, fetching a page is a deliberate act: it is a source
    even when the sentence it informed carries no link."""
    item_ids = await seed_items(session_factory, 1)

    async def fake_article(url, *_args, **_kwargs):
        return extract_service.ExtractResult(ok=True, text="advisory body")

    monkeypatch.setattr(extract_service, "extract_article", fake_article)
    use_script(
        app,
        turn_tool_use([("fetch_article", {"url": "https://vendor.test/unlinked"})]),
        turn_text("## Story 0\nNo links at all."),
    )

    await generate(client, item_ids=item_ids)

    sources = await source_rows(session_factory)
    assert [source.url for source in sources] == [
        "https://example.test/story-0",
        "https://vendor.test/unlinked",
    ]


# ------------------------------------------------------------------ errors


async def test_a_refusal_saves_nothing_and_never_emits_done(
    app, client, with_key, session_factory
):
    item_ids = await seed_items(session_factory, 1)
    use_script(app, turn_refusal(category="cyber", explanation="declined"))

    response = await generate(client, item_ids=item_ids)

    events = parse_sse(response.text)
    names = [name for name, _ in events]
    assert "error" in names
    assert "done" not in names
    error = next(payload for name, payload in events if name == "error")
    assert error["type"] == "refusal"
    assert error["category"] == "cyber"

    assert await note_rows(session_factory) == []
    assert await source_rows(session_factory) == []


async def test_a_turn_cap_hit_saves_nothing(app, client, with_key, session_factory, db_session):
    await settings_service.set_value(db_session, "max_tool_turns", "1")
    await db_session.commit()
    item_ids = await seed_items(session_factory, 1)
    use_script(
        app,
        turn_tool_use([("get_feed_item", {"item_id": item_ids[0]})]),
        turn_tool_use([("get_feed_item", {"item_id": item_ids[0]})]),
    )

    response = await generate(client, item_ids=item_ids)

    events = parse_sse(response.text)
    assert "done" not in [name for name, _ in events]
    assert next(payload for name, payload in events if name == "error")["type"] == "turn_limit"
    assert await note_rows(session_factory) == []


async def test_no_api_key_is_a_single_error_event(app, client, session_factory):
    item_ids = await seed_items(session_factory, 1)

    response = await generate(client, item_ids=item_ids)

    events = parse_sse(response.text)
    assert [name for name, _ in events] == ["error"]
    assert events[0][1]["type"] == "api_error"
    assert await note_rows(session_factory) == []


# --------------------------------------------------------------- cancelling


async def test_cancel_stops_the_generation_and_saves_nothing(app, with_key, session_factory):
    item_ids = await seed_items(session_factory, 1)
    scripted = ScriptedAnthropic([turn_text("half a note", delay_s=0.05)])
    app.dependency_overrides[get_chat_client_factory] = lambda: lambda _key: scripted

    async with httpx2.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        stream = asyncio.create_task(
            http.post(
                "/api/notes/generate",
                json={"item_ids": item_ids, "generation_id": "gen-cancel"},
            )
        )
        await asyncio.sleep(0.08)
        cancelled = await http.post(
            "/api/notes/generate/cancel", json={"generation_id": "gen-cancel"}
        )
        assert cancelled.json() == {"cancelled": True}

        response = await stream

    events = parse_sse(response.text)
    names = [name for name, _ in events]
    assert "done" not in names
    assert next(payload for name, payload in events if name == "error")["type"] == "cancelled"
    assert await note_rows(session_factory) == []


async def test_a_second_generation_under_the_same_id_is_a_conflict(
    app, with_key, session_factory
):
    item_ids = await seed_items(session_factory, 1)
    scripted = ScriptedAnthropic([turn_text("slow", delay_s=0.05), turn_text("second")])
    app.dependency_overrides[get_chat_client_factory] = lambda: lambda _key: scripted

    async with httpx2.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        body = {"item_ids": item_ids, "generation_id": "gen-dupe"}
        first = asyncio.create_task(http.post("/api/notes/generate", json=body))
        await asyncio.sleep(0.05)
        second = await http.post("/api/notes/generate", json=body)
        assert second.status_code == 409
        await first


def test_the_scripted_tool_input_is_json(  # sanity: the fake really streams JSON fragments
) -> None:
    turn = turn_tool_use([("fetch_article", {"url": "https://vendor.test/a"})])
    fragments = "".join(
        event.delta.partial_json
        for event in turn.events
        if getattr(event, "type", "") == "content_block_delta"
    )
    assert json.loads(fragments) == {"url": "https://vendor.test/a"}


async def test_mcp_tools_are_never_offered_to_note_generation(
    app, client, with_key, session_factory
):
    """Even with a reachable server configured, notes stay on the local tools."""
    from fakes.mcp import SpyFactory, build_server

    from app.mcp.manager import McpManager

    manager = McpManager(target_factory=SpyFactory({"files": build_server()}))
    app.state.mcp_manager = manager
    try:
        await client.put("/api/mcp/servers", json={"mcpServers": {"files": {"command": "fixture"}}})
        # The chat turn sees the server's tools...
        chat = ScriptedAnthropic([turn_text("ok")])
        app.dependency_overrides[get_chat_client_factory] = lambda: lambda _key: chat
        created = await client.post("/api/sessions", json={})
        await client.post(
            f"/api/sessions/{created.json()['id']}/messages", json={"content": "hello"}
        )
        assert any(
            tool["name"].startswith("mcp__") for tool in chat.calls[0]["tools"]
        ), "the MCP fixture server should be reachable for this test to mean anything"

        # ...and the generation does not.
        item_ids = await seed_items(session_factory, 1)
        scripted = use_script(app, turn_text("notes"))
        await generate(client, item_ids=item_ids)
    finally:
        await manager.aclose()

    names = [definition["name"] for definition in scripted.calls[0]["tools"]]
    assert names == ["fetch_article", "get_feed_item", "web_search"]


async def test_a_long_transcript_keeps_its_most_recent_end(
    app, client, with_key, session_factory
):
    async with session_factory() as session:
        research = ResearchSession(title="Long thread", model="claude-opus-5")
        session.add(research)
        await session.flush()
        session.add_all(
            [
                Message(
                    session_id=research.id,
                    seq=1,
                    role="user",
                    kind="user",
                    content_json=[
                        {"type": "text", "text": "OLDEST-LINE " + "padding " * 8000}
                    ],
                ),
                Message(
                    session_id=research.id,
                    seq=2,
                    role="assistant",
                    kind="assistant",
                    content_json=[{"type": "text", "text": "NEWEST-LINE"}],
                    stop_reason="end_turn",
                ),
            ]
        )
        await session.commit()
        session_id = research.id

    scripted = use_script(app, turn_text("## notes"))
    await generate(client, session_id=session_id)

    text = user_text(scripted)
    assert "NEWEST-LINE" in text
    assert "OLDEST-LINE" not in text
    assert notes_service.TRANSCRIPT_TRUNCATION_PREFIX.strip() in text
