"""Key/value settings store and the API-key masking helper."""

import logging
from typing import get_args

import pytest

from app.agent.providers import turn_settings
from app.config import Settings
from app.schemas.settings import CompileMode, Effort, ThinkingDisplay
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


# ------------------------------------------ the key from .env (app Settings)


async def test_effective_api_key_falls_back_to_the_dotenv_key(db_session):
    """A key written into ``.env`` reaches us as ``Settings``, never ``os.environ``.

    pydantic-settings loads ``.env`` into its own fields and does not export it, so
    reading the environment alone silently ignored it and the app said "no API key
    configured" to someone who had followed the README.
    """
    dotenv = Settings(anthropic_api_key="sk-ant-from-dotenv-7777")

    assert await settings_service.get_effective_api_key(db_session, dotenv) == (
        "sk-ant-from-dotenv-7777"
    )
    # Without the Settings source there is nothing to find.
    assert await settings_service.get_effective_api_key(db_session) == ""


async def test_effective_api_key_precedence_is_env_then_dotenv_then_stored(
    db_session, monkeypatch
):
    dotenv = Settings(anthropic_api_key="sk-ant-from-dotenv-7777")
    await settings_service.set_value(db_session, "anthropic_api_key", "sk-ant-stored1234")

    # Stored is last: .env beats it...
    assert await settings_service.get_effective_api_key(db_session, dotenv) == (
        "sk-ant-from-dotenv-7777"
    )
    # ...and the real process environment beats .env.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fromenv1234")
    assert await settings_service.get_effective_api_key(db_session, dotenv) == (
        "sk-ant-fromenv1234"
    )
    # A blank .env line is not an override.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    blank = Settings(anthropic_api_key="   ")
    assert await settings_service.get_effective_api_key(db_session, blank) == "sk-ant-stored1234"


async def test_the_external_key_is_never_written_to_the_database(db_session):
    dotenv = Settings(anthropic_api_key="sk-ant-from-dotenv-7777")

    assert await settings_service.get_effective_api_key(db_session, dotenv)

    assert await settings_service.get(db_session, "anthropic_api_key") == ""


async def test_key_source_names_the_winning_source(db_session, monkeypatch):
    empty = Settings(anthropic_api_key="")
    assert await settings_service.get_key_source(db_session, empty) == "none"

    await settings_service.set_value(db_session, "anthropic_api_key", "sk-ant-stored1234")
    assert await settings_service.get_key_source(db_session, empty) == "stored"

    dotenv = Settings(anthropic_api_key="sk-ant-from-dotenv-7777")
    assert await settings_service.get_key_source(db_session, dotenv) == "env"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fromenv1234")
    assert await settings_service.get_key_source(db_session, empty) == "env"


# ------------------------------------------- values from a closed set (get_choice)


def test_the_allowed_values_are_the_ones_the_api_promises():
    """The store is TEXT, so the coercion needs its own list — but a list that
    drifts from the response model would coerce a value the API then rejects."""
    assert settings_service.ALLOWED_VALUES["effort"] == get_args(Effort)
    assert settings_service.ALLOWED_VALUES["thinking_display"] == get_args(ThinkingDisplay)
    assert settings_service.ALLOWED_VALUES["kb_compile_effort"] == get_args(Effort)
    assert settings_service.ALLOWED_VALUES["kb_compile_mode"] == get_args(CompileMode)


async def test_get_choice_returns_a_stored_value_from_the_set(db_session):
    await settings_service.set_value(db_session, "effort", "low")

    assert await settings_service.get_choice(db_session, "effort") == "low"


@pytest.mark.parametrize(
    ("key", "stored", "expected"),
    [("effort", "turbo", "high"), ("thinking_display", "loud", "summarized")],
)
async def test_get_choice_falls_an_unknown_value_back_to_the_default(
    db_session, caplog, key, stored, expected
):
    await settings_service.set_value(db_session, key, stored)

    with caplog.at_level(logging.WARNING, logger="app.services.settings"):
        assert await settings_service.get_choice(db_session, key) == expected

    # Silent coercion is the failure mode: the row is left alone, so the only
    # trace that the stored value is being ignored is this line.
    assert key in caplog.text
    assert "falling back" in caplog.text
    # Reading is not repairing.
    assert await settings_service.get(db_session, key) == stored


async def test_a_turn_reads_the_same_coerced_values_as_the_settings_page(db_session):
    """The bug: `GET /api/settings` coerced `effort` for display while the turn
    sent the raw value, so the page said "high" and every message 400'd."""
    await settings_service.set_value(db_session, "effort", "turbo")
    await settings_service.set_value(db_session, "thinking_display", "loud")

    resolved = await turn_settings(db_session)

    assert resolved["effort"] == "high"
    assert resolved["thinking_display"] == "summarized"


# --------------------------------------------------------------- the Voyage key


async def test_effective_voyage_key_precedence_is_env_then_dotenv_then_stored(
    db_session, monkeypatch
):
    dotenv = Settings(voyage_api_key="pa-from-dotenv-7777")
    await settings_service.set_value(db_session, "voyage_api_key", "pa-stored-1234")

    assert await settings_service.get_effective_voyage_key(db_session) == "pa-stored-1234"
    assert await settings_service.get_effective_voyage_key(db_session, dotenv) == (
        "pa-from-dotenv-7777"
    )

    monkeypatch.setenv("VOYAGE_API_KEY", "pa-from-env-9999")
    assert await settings_service.get_effective_voyage_key(db_session, dotenv) == (
        "pa-from-env-9999"
    )

    # An empty env var is not an override.
    monkeypatch.setenv("VOYAGE_API_KEY", "")
    assert await settings_service.get_effective_voyage_key(db_session, dotenv) == (
        "pa-from-dotenv-7777"
    )


async def test_the_external_voyage_key_is_never_written_to_the_database(db_session):
    dotenv = Settings(voyage_api_key="pa-from-dotenv-7777")

    assert await settings_service.get_effective_voyage_key(db_session, dotenv)

    assert await settings_service.get(db_session, "voyage_api_key") == ""


async def test_voyage_key_source_names_the_winning_source(db_session, monkeypatch):
    empty = Settings(voyage_api_key="")
    assert await settings_service.get_voyage_key_source(db_session, empty) == "none"

    await settings_service.set_value(db_session, "voyage_api_key", "pa-stored-1234")
    assert await settings_service.get_voyage_key_source(db_session, empty) == "stored"

    dotenv = Settings(voyage_api_key="pa-from-dotenv-7777")
    assert await settings_service.get_voyage_key_source(db_session, dotenv) == "env"

    monkeypatch.setenv("VOYAGE_API_KEY", "pa-from-env-9999")
    assert await settings_service.get_voyage_key_source(db_session, empty) == "env"


# ------------------------------------------------------- the Phase 2 settings


async def test_the_phase_two_defaults_are_seeded(db_session):
    assert await settings_service.get(db_session, "voyage_api_key") == ""
    assert await settings_service.get_str(db_session, "kb_embedding_model") == "voyage-4"
    assert await settings_service.get_bool(db_session, "kb_capture_findings") is False
    assert await settings_service.get_choice(db_session, "kb_compile_mode") == "manual"
    assert await settings_service.get_str(db_session, "kb_compile_model") == "claude-sonnet-5"
    assert await settings_service.get_choice(db_session, "kb_compile_effort") == "low"
    assert await settings_service.get_int(db_session, "kb_compile_max_chars") == 24_000
    assert (
        await settings_service.get_int(db_session, "kb_compile_monthly_token_budget") == 5_000_000
    )
    assert await settings_service.get_bool(db_session, "kb_auto_accept_suggestions") is True
    assert await settings_service.get_bool(db_session, "kb_reviewed_only") is False
    assert await settings_service.get_bool(db_session, "kb_recency_boost") is True
    assert await settings_service.get_bool(db_session, "kb_rerank") is True
    assert await settings_service.get_float(db_session, "kb_duplicate_threshold") == 0.92
    assert (
        await settings_service.get_str(db_session, "kb_compile_prompt")
        == settings_service.DEFAULT_COMPILE_PROMPT
    )


async def test_get_float_falls_back_on_garbage(db_session):
    await settings_service.set_value(db_session, "kb_duplicate_threshold", "0.5")
    assert await settings_service.get_float(db_session, "kb_duplicate_threshold") == 0.5

    await settings_service.set_value(db_session, "kb_duplicate_threshold", "nearly")
    assert await settings_service.get_float(db_session, "kb_duplicate_threshold") == 0.92
    # A key with no documented default is 0.0 rather than an exception.
    assert await settings_service.get_float(db_session, "nope") == 0.0


def test_the_compile_prompt_is_generic_and_says_the_text_is_data():
    """Ground rule: no employer or company context in a shipped prompt, and the
    article is material to summarise rather than instructions to obey."""
    prompt = settings_service.DEFAULT_COMPILE_PROMPT

    assert isinstance(settings_service.COMPILE_PROMPT_VERSION, int)
    assert "instruction" in prompt.lower()
    assert prompt == prompt.strip()
    # The ground rule itself, not just the shape: nothing that would make the
    # prompt about a particular employer, product or customer.
    forbidden = ("employer", "company", "corporate", "organisation", "organization", "our ")
    assert [word for word in forbidden if word in prompt.lower()] == []


def test_the_minimum_snapshot_default_matches_the_capture_constant():
    """The settings module cannot import the constant — it is imported *by* the
    capture path — so the two are pinned here instead, the same way
    ``ALLOWED_VALUES`` is pinned against the API's ``Literal``s."""
    from app.kb.capture import DEFAULT_MIN_SNAPSHOT_CHARS

    assert settings_service.DEFAULT_SETTINGS["kb_min_snapshot_chars"] == str(
        DEFAULT_MIN_SNAPSHOT_CHARS
    )
