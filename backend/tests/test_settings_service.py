"""Key/value settings store and the API-key masking helper."""

import pytest

from app.services import settings as settings_service


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sk-ant-api03-abcdefghijklmnop-a1b2", "sk-ant-…a1b2"),
        ("sk-ant-test12345", "sk-ant-…2345"),
        ("", ""),
        ("   ", ""),
        # Not an Anthropic-shaped key: still masked, no prefix invented.
        ("0123456789abcdef", "…cdef"),
        # Too short to reveal anything without leaking most of it.
        ("sk-ant-1", "sk-ant-…"),
        ("abc", "…"),
    ],
)
def test_mask_key(raw, expected):
    assert settings_service.mask_key(raw) == expected


def test_mask_key_never_contains_the_whole_key():
    raw = "sk-ant-api03-" + "x" * 60 + "wxyz"
    masked = settings_service.mask_key(raw)
    assert raw not in masked
    assert masked == "sk-ant-…wxyz"


async def test_defaults_are_seeded(db_session):
    assert await settings_service.get(db_session, "model") == "claude-opus-5"
    assert await settings_service.get(db_session, "effort") == "high"
    assert await settings_service.get(db_session, "anthropic_api_key") == ""
    assert "**What happened**" in await settings_service.get(db_session, "note_template")


async def test_get_returns_default_for_unknown_key(db_session):
    assert await settings_service.get(db_session, "nope") is None
    assert await settings_service.get(db_session, "nope", "fallback") == "fallback"


async def test_set_many_updates_and_inserts(db_session):
    await settings_service.set_many(db_session, {"model": "claude-sonnet-5", "brand_new": "yes"})
    await db_session.commit()

    assert await settings_service.get(db_session, "model") == "claude-sonnet-5"
    assert await settings_service.get(db_session, "brand_new") == "yes"
    # Untouched keys keep their seeded values.
    assert await settings_service.get(db_session, "effort") == "high"


async def test_typed_accessors(db_session):
    assert await settings_service.get_bool(db_session, "web_search_enabled") is True
    assert await settings_service.get_int(db_session, "web_search_max_uses") == 8
    assert await settings_service.get_int(db_session, "max_tool_turns") == 12
    assert await settings_service.get_int(db_session, "feed_timeout_s") == 15

    await settings_service.set_value(db_session, "web_search_enabled", "false")
    assert await settings_service.get_bool(db_session, "web_search_enabled") is False


async def test_typed_accessors_fall_back_on_garbage(db_session):
    await settings_service.set_many(
        db_session, {"web_search_max_uses": "not-a-number", "web_fetch_enabled": "maybe"}
    )

    # A corrupt row must not take the app down; the documented default wins.
    assert await settings_service.get_int(db_session, "web_search_max_uses") == 8
    assert await settings_service.get_bool(db_session, "web_fetch_enabled") is True


async def test_effective_api_key_prefers_the_environment(db_session, monkeypatch):
    await settings_service.set_value(db_session, "anthropic_api_key", "sk-ant-stored1234")

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert await settings_service.get_effective_api_key(db_session) == "sk-ant-stored1234"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fromenv1234")
    assert await settings_service.get_effective_api_key(db_session) == "sk-ant-fromenv1234"

    # An empty env var is not an override.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert await settings_service.get_effective_api_key(db_session) == "sk-ant-stored1234"


async def test_get_all_round_trips(db_session):
    stored = await settings_service.get_all(db_session)
    assert set(stored) == set(settings_service.DEFAULT_SETTINGS)
