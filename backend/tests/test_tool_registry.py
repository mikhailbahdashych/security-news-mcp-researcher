"""The tool registry: ordering, gating, filtering, dispatch safety."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.agent.builtin import MAX_SEARCH_LIMIT, BuiltinToolProvider, ServerToolProvider
from app.agent.registry import (
    TOOL_NAME_PATTERN,
    RegisteredTool,
    ToolRegistry,
    ToolResult,
    ToolSource,
    sanitize_tool_name,
)
from app.db.models import Feed, FeedItem, utcnow
from app.services import settings as settings_service


@dataclass
class ListProvider:
    tools_: list[RegisteredTool]
    source: ToolSource = ToolSource.MCP

    async def list_tools(self) -> list[RegisteredTool]:
        return list(self.tools_)


def tool(name: str, *, handler=None, source: ToolSource = ToolSource.MCP) -> RegisteredTool:
    return RegisteredTool(
        name=name,
        source=source,
        definition={"name": name, "description": "d", "input_schema": {"type": "object"}},
        handler=handler,
    )


def registry(session_factory, **overrides) -> ToolRegistry:
    server = ServerToolProvider(
        web_search_enabled=overrides.pop("web_search_enabled", True),
        web_search_max_uses=overrides.pop("web_search_max_uses", 8),
        web_fetch_enabled=overrides.pop("web_fetch_enabled", True),
    )
    # Task 5 appends its McpToolProvider to this list — nothing else changes.
    return ToolRegistry([BuiltinToolProvider(session_factory), server])


async def test_ordering_is_stable_across_calls_and_rebuilds(session_factory):
    first = await registry(session_factory).definitions()
    second = await registry(session_factory).definitions()
    one_registry = registry(session_factory)

    assert first == second
    assert await one_registry.definitions() == await one_registry.definitions()
    assert [definition["name"] for definition in first] == [
        # builtin, name-ascending...
        "fetch_article",
        "get_feed_item",
        "get_kb_entry",
        "search_feed_items",
        "search_knowledge_base",
        # ...then server, in fixed literal order.
        "web_search",
        "web_fetch",
    ]


async def test_server_tools_absent_when_both_toggles_are_off(session_factory):
    definitions = await registry(
        session_factory, web_search_enabled=False, web_fetch_enabled=False
    ).definitions()

    assert [definition["name"] for definition in definitions] == [
        "fetch_article",
        "get_feed_item",
        "get_kb_entry",
        "search_feed_items",
        "search_knowledge_base",
    ]
    assert not any("type" in definition for definition in definitions)


async def test_server_tool_types_and_max_uses(session_factory):
    definitions = await registry(session_factory, web_search_max_uses=3).definitions()
    by_name = {definition["name"]: definition for definition in definitions if "type" in definition}

    assert by_name["web_search"] == {
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": 3,
    }
    assert by_name["web_fetch"] == {"type": "web_fetch_20260209", "name": "web_fetch"}


async def test_server_tool_provider_reads_the_settings(db_session, session_factory):
    await settings_service.set_many(
        db_session, {"web_search_enabled": "false", "web_search_max_uses": "2"}
    )
    await db_session.commit()

    provider = await ServerToolProvider.from_settings(db_session)

    assert provider.web_search_enabled is False
    assert provider.web_search_max_uses == 2
    assert [t.name for t in await provider.list_tools()] == ["web_fetch"]


async def test_tool_subset_filters_and_ignores_unknown_names(session_factory):
    names = [
        definition["name"]
        for definition in await registry(session_factory).definitions(
            subset={"get_feed_item", "web_search", "mcp__nope__tool"}
        )
    ]

    assert names == ["get_feed_item", "web_search"]


async def test_unknown_tool_dispatch_returns_an_error_result(session_factory):
    result = await registry(session_factory).dispatch("no_such_tool", {})

    assert result.is_error is True
    assert "Unknown tool" in result.content


async def test_dispatching_a_server_tool_is_an_error_not_a_crash(session_factory):
    result = await registry(session_factory).dispatch("web_search", {"query": "x"})

    assert result.is_error is True


async def test_a_crashing_handler_becomes_an_error_result(session_factory):
    async def boom(**_kwargs: object) -> ToolResult:
        raise RuntimeError("kaboom")

    result = await ToolRegistry([ListProvider([tool("t", handler=boom)])]).dispatch("t", {})

    assert result.is_error is True
    assert "kaboom" in result.content


async def test_bad_arguments_become_an_error_result(session_factory):
    async def handler(expected: str) -> ToolResult:
        return ToolResult(content=expected)

    result = await ToolRegistry([ListProvider([tool("t", handler=handler)])]).dispatch(
        "t", {"invented": 1}
    )

    assert result.is_error is True
    assert "invalid arguments" in result.content


async def test_a_hanging_handler_times_out(session_factory):
    async def handler(**_kwargs: object) -> ToolResult:
        await asyncio.sleep(10)
        return ToolResult(content="never")

    result = await ToolRegistry(
        [ListProvider([tool("t", handler=handler)])], timeout_s=0.01
    ).dispatch("t", {})

    assert result.is_error is True
    assert "timed out" in result.content


async def test_cancellation_is_not_swallowed_into_an_error_result():
    async def handler(**_kwargs: object) -> ToolResult:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await ToolRegistry([ListProvider([tool("t", handler=handler)])]).dispatch("t", {})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("mcp__server__tool", "mcp__server__tool"),
        ("weather.today", "weather_today"),
        ("has spaces", "has_spaces"),
        ("!!!", "___"),
        ("", "_"),
        ("x" * 200, "x" * 128),
    ],
)
def test_name_sanitization(raw, expected):
    sanitized = sanitize_tool_name(raw)
    assert sanitized == expected
    assert TOOL_NAME_PATTERN.match(sanitized)


async def test_duplicate_names_keep_the_first_registration():
    first = tool("dup")
    second = RegisteredTool(
        name="dup", source=ToolSource.MCP, definition={"name": "dup", "marker": True}
    )

    tools = await ToolRegistry([ListProvider([first]), ListProvider([second])]).tools()

    assert [t.definition for t in tools] == [first.definition]


async def test_a_sanitized_name_is_applied_to_the_definition_too():
    tools = await ToolRegistry([ListProvider([tool("weather.today")])]).tools()

    assert tools[0].name == "weather_today"
    assert tools[0].definition["name"] == "weather_today"


# --------------------------------------------------------- builtin behaviour


@pytest.fixture
async def seeded(db_session):
    feed = Feed(url="https://example.test/rss", title="Example Security")
    db_session.add(feed)
    await db_session.flush()
    db_session.add_all(
        [
            FeedItem(
                feed_id=feed.id,
                guid="g1",
                url="https://example.test/a",
                title="CVE-2026-1234 in AcmeVPN",
                summary="A pre-auth RCE in AcmeVPN.",
                content_text="Full body about AcmeVPN.",
                published_at=utcnow(),
                status="starred",
            ),
            FeedItem(
                feed_id=feed.id,
                guid="g2",
                url="https://example.test/b",
                title="Unrelated phishing wave",
                summary="Nothing to do with the above.",
                published_at=utcnow(),
                status="unread",
            ),
        ]
    )
    await db_session.commit()
    return feed


async def test_search_feed_items_renders_a_compact_list(session_factory, seeded):
    provider = BuiltinToolProvider(session_factory)

    result = await provider.search_feed_items(q="AcmeVPN")

    assert result.is_error is False
    assert "CVE-2026-1234 in AcmeVPN" in result.content
    assert "Example Security" in result.content
    assert "Unrelated phishing" not in result.content


async def test_search_feed_items_finds_text_only_in_the_article_body(
    session_factory, seeded, db_session
):
    """The agent has to be able to find a CVE that only the extracted article names.

    ``get_feed_item`` needs an id, and the only way to get one is this tool — so a
    search that ignores ``content_text`` makes an extracted article unreachable.
    """
    item = (
        await db_session.execute(select(FeedItem).where(FeedItem.guid == "g2"))
    ).scalar_one()
    item.content_text = "Buried in paragraph nine: CVE-2026-99999 affects the console."
    await db_session.commit()

    result = await BuiltinToolProvider(session_factory).search_feed_items(q="CVE-2026-99999")

    assert result.is_error is False
    assert "Unrelated phishing wave" in result.content


async def test_search_feed_items_maps_any_to_all(session_factory, seeded):
    result = await BuiltinToolProvider(session_factory).search_feed_items(q="AcmeVPN", status="any")

    assert result.is_error is False
    assert "AcmeVPN" in result.content


async def test_search_feed_items_says_nothing_about_more_when_it_returned_everything(
    session_factory, seeded
):
    result = await BuiltinToolProvider(session_factory).search_feed_items(q="AcmeVPN")

    assert "further matches" not in result.content
    assert "match in total" not in result.content
    assert result.raw == {"count": 1, "has_more": False, "next_cursor": None}


async def test_search_feed_items_admits_the_matches_the_limit_cut(session_factory, seeded):
    """Silence read as "that is the whole inbox", which is how a search that
    matched far more than it showed became a confident summary of the first few."""
    result = await BuiltinToolProvider(session_factory).search_feed_items(q="e", limit=1)

    assert result.is_error is False
    assert "further matches were cut by limit" in result.content
    assert result.raw["has_more"] is True
    assert result.raw["count"] == 1


async def test_search_feed_items_admits_a_full_page(session_factory, db_session, seeded):
    """More matches than one page holds: the tool takes no cursor, so the model
    is told to narrow the query rather than handed one it cannot use."""
    feed_id = seeded.id
    db_session.add_all(
        [
            FeedItem(
                feed_id=feed_id,
                guid=f"bulk-{index}",
                url=f"https://example.test/bulk-{index}",
                title=f"AcmeVPN follow-up {index}",
                summary="More AcmeVPN coverage.",
                published_at=utcnow(),
            )
            for index in range(MAX_SEARCH_LIMIT)
        ]
    )
    await db_session.commit()

    result = await BuiltinToolProvider(session_factory).search_feed_items(
        q="AcmeVPN", limit=MAX_SEARCH_LIMIT
    )

    assert f"More than {MAX_SEARCH_LIMIT} items match" in result.content
    assert result.raw["has_more"] is True
    assert result.raw["next_cursor"]


def bulk_items(feed_id: int, count: int, *, prefix: str, summary: str, age_days: int = 0):
    """`count` matching items, all `age_days` old (0 = now)."""
    published = utcnow() - timedelta(days=age_days)
    return [
        FeedItem(
            feed_id=feed_id,
            guid=f"{prefix}-{index}",
            url=f"https://example.test/{prefix}-{index}",
            title=f"AcmeVPN advisory {index}",
            summary=summary,
            published_at=published,
        )
        for index in range(count)
    ]


async def test_search_feed_items_counts_what_it_actually_rendered(
    session_factory, db_session, seeded
):
    """The renderer has its own character budget, so a `count` taken before it
    ran described an answer the model was never shown all of — and the stored
    tool_call row is the only record of what the turn really saw."""
    db_session.add_all(bulk_items(seeded.id, 40, prefix="wordy", summary="AcmeVPN detail. " * 30))
    await db_session.commit()

    result = await BuiltinToolProvider(session_factory).search_feed_items(
        q="AcmeVPN", limit=MAX_SEARCH_LIMIT
    )

    rendered = result.content.count("[id ")
    assert "omitted to save space" in result.content
    assert rendered < 41
    assert result.raw["count"] == rendered
    assert result.raw["has_more"] is True


async def test_search_feed_items_does_not_warn_about_items_the_window_excludes(
    session_factory, db_session, seeded
):
    """`next_cursor` describes the query *without* the date bound. Items come
    back newest-first, so once the window has cut into the page, everything
    beyond the page is older still — and "bound it with since_days" is advice
    the model has already taken."""
    db_session.add_all(
        bulk_items(
            seeded.id,
            MAX_SEARCH_LIMIT + 5,
            prefix="old",
            summary="Older AcmeVPN coverage.",
            age_days=30,
        )
    )
    await db_session.commit()

    result = await BuiltinToolProvider(session_factory).search_feed_items(q="AcmeVPN", since_days=7)

    assert f"More than {MAX_SEARCH_LIMIT} items match" not in result.content
    assert result.raw["has_more"] is False
    assert result.raw["count"] == 1


async def test_a_window_that_excludes_nothing_still_warns_about_the_page(
    session_factory, db_session, seeded
):
    """The other half: a `since_days` wide enough to keep the whole page leaves
    the warning standing, because there really is more behind it."""
    db_session.add_all(
        bulk_items(seeded.id, MAX_SEARCH_LIMIT + 5, prefix="recent", summary="AcmeVPN coverage.")
    )
    await db_session.commit()

    result = await BuiltinToolProvider(session_factory).search_feed_items(
        q="AcmeVPN", since_days=30, limit=MAX_SEARCH_LIMIT
    )

    assert f"More than {MAX_SEARCH_LIMIT} items match" in result.content
    assert result.raw["has_more"] is True


async def test_search_feed_items_empty_result_is_not_an_error(session_factory, seeded):
    result = await BuiltinToolProvider(session_factory).search_feed_items(q="zzzz")

    assert result.is_error is False
    assert 'No items in the local inbox match "zzzz".' in result.content
    assert "web_search" in result.content


async def test_get_feed_item_returns_stored_text(session_factory, seeded, db_session):
    from sqlalchemy import select

    item_id = await db_session.scalar(select(FeedItem.id).where(FeedItem.guid == "g1"))

    result = await BuiltinToolProvider(session_factory).get_feed_item(item_id=item_id)

    assert result.is_error is False
    assert "Full body about AcmeVPN." in result.content


async def test_get_feed_item_unknown_id_is_an_error(session_factory, seeded):
    result = await BuiltinToolProvider(session_factory).get_feed_item(item_id=9999)

    assert result.is_error is True
    assert "search_feed_items" in result.content


async def test_fetch_article_rejects_a_non_http_scheme(session_factory):
    result = await BuiltinToolProvider(session_factory).fetch_article(url="file:///etc/passwd")

    assert result.is_error is True
    assert "absolute http(s) URL" in result.content
