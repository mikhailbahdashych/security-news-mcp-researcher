"""Parsing, validating and persisting the ``mcpServers`` blob."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import McpServer, McpToolPref
from app.mcp.config import (
    McpConfigError,
    load_servers,
    parse_config,
    redact,
    redact_text,
    save_servers,
    to_public_json,
)

STDIO = {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/x"]}
HTTP = {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer s3cret"}}


def test_stdio_and_http_entries_get_their_transports() -> None:
    servers = parse_config({"mcpServers": {"files": STDIO, "remote": HTTP}})

    assert [s.name for s in servers] == ["files", "remote"]
    assert servers[0].transport == "stdio"
    assert servers[0].command == "npx"
    assert servers[0].args == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/x"]
    assert servers[1].transport == "http"
    assert servers[1].headers == {"Authorization": "Bearer s3cret"}


def test_empty_config_is_valid() -> None:
    assert parse_config({"mcpServers": {}}) == []
    assert parse_config({}) == []


@pytest.mark.parametrize(
    ("blob", "fragment"),
    [
        ({"mcpServers": {"both": {**STDIO, **HTTP}}}, "not both"),
        ({"mcpServers": {"neither": {"args": ["x"]}}}, 'either "command"'),
        ({"mcpServers": {"typo": {"comand": "npx"}}}, 'unknown field "comand"'),
        ({"mcpServers": {"bad name": STDIO}}, "invalid name"),
        ({"mcpServers": {"scheme": {"url": "ftp://example.com"}}}, "http://"),
        ({"mcpServers": {"notobj": "npx"}}, "must be a JSON object"),
    ],
)
def test_invalid_entries_are_rejected_by_name(blob: dict, fragment: str) -> None:
    with pytest.raises(McpConfigError) as excinfo:
        parse_config(blob)

    message = str(excinfo.value)
    assert fragment in message
    # The message has to say *which* server is wrong; a bare "validation error" is
    # useless when the blob has six servers in it.
    assert next(iter(blob["mcpServers"])) in message


def test_unknown_top_level_key_is_rejected() -> None:
    with pytest.raises(McpConfigError, match="mcpServers"):
        parse_config({"servers": {"files": STDIO}})


async def test_save_then_load_round_trips(db_session: AsyncSession) -> None:
    result = await save_servers(db_session, {"mcpServers": {"files": STDIO, "remote": HTTP}})

    assert result.added == ["files", "remote"]
    assert result.removed == []

    loaded = await load_servers(db_session)
    assert [s.name for s in loaded] == ["files", "remote"]
    assert to_public_json(loaded) == {
        "mcpServers": {
            "files": {"command": "npx", "args": STDIO["args"]},
            "remote": {"url": HTTP["url"], "headers": HTTP["headers"]},
        }
    }


async def test_dropping_a_server_deletes_its_tool_prefs(db_session: AsyncSession) -> None:
    await save_servers(db_session, {"mcpServers": {"files": STDIO, "remote": HTTP}})
    db_session.add(McpToolPref(server_name="files", tool_name="read_file", enabled=False))
    db_session.add(McpToolPref(server_name="remote", tool_name="search", enabled=False))
    await db_session.commit()

    result = await save_servers(db_session, {"mcpServers": {"remote": HTTP}})

    assert result.removed == ["files"]
    prefs = (await db_session.execute(select(McpToolPref))).scalars().all()
    assert [(p.server_name, p.tool_name) for p in prefs] == [("remote", "search")]


async def test_changed_server_is_reported_as_updated(db_session: AsyncSession) -> None:
    await save_servers(db_session, {"mcpServers": {"files": STDIO}})

    unchanged = await save_servers(db_session, {"mcpServers": {"files": STDIO}})
    assert unchanged.updated == []

    changed = await save_servers(
        db_session, {"mcpServers": {"files": {**STDIO, "args": ["-y", "other"]}}}
    )
    assert changed.updated == ["files"]


async def test_nothing_is_persisted_when_validation_fails(db_session: AsyncSession) -> None:
    await save_servers(db_session, {"mcpServers": {"files": STDIO}})

    with pytest.raises(McpConfigError):
        await save_servers(db_session, {"mcpServers": {"files": STDIO, "broken": {}}})

    rows = (await db_session.execute(select(McpServer))).scalars().all()
    assert [row.name for row in rows] == ["files"]


async def test_disabled_flag_round_trips(db_session: AsyncSession) -> None:
    await save_servers(db_session, {"mcpServers": {"files": {**STDIO, "enabled": False}}})

    loaded = await load_servers(db_session)
    assert loaded[0].enabled is False
    assert to_public_json(loaded)["mcpServers"]["files"]["enabled"] is False


def test_redact_keeps_keys_and_drops_values() -> None:
    assert redact({"Authorization": "Bearer s3cret"}) == {"Authorization": "***"}


def test_repr_never_leaks_a_header_value() -> None:
    config = parse_config({"mcpServers": {"remote": HTTP}})[0]
    assert "s3cret" not in repr(config)


def test_repr_never_leaks_a_token_in_the_url() -> None:
    """A repr ends up in tracebacks, and a hosted server's credential is as
    likely to be in the query string as in a header."""
    config = parse_config(
        {"mcpServers": {"remote": {"url": "https://example.com/mcp?key=s3cret"}}}
    )[0]

    rendered = repr(config)

    assert "s3cret" not in rendered
    # Still says which server it is: the point is diagnosis, not silence.
    assert "https://example.com/mcp?***" in rendered


def test_redact_text_takes_out_userinfo_and_query_strings() -> None:
    assert redact_text("https://user:pw@example.com/mcp") == "https://***@example.com/mcp"
    assert redact_text("GET https://example.com/mcp?token=abc failed") == (
        "GET https://example.com/mcp?*** failed"
    )
    # Nothing to hide, nothing changed — including a bare question mark in prose.
    assert redact_text("connection refused. Is the server running?") == (
        "connection refused. Is the server running?"
    )
