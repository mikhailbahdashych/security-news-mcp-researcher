"""Settings endpoints.

The stored Anthropic API key is write-only across this whole module: it can be set
through ``PUT /api/settings`` and is only ever read back masked.
"""

from __future__ import annotations

import logging
from typing import Any, get_args

from fastapi import APIRouter

from app.api.deps import AnthropicClient, AppSettings, DbSession
from app.schemas.settings import (
    Effort,
    SettingsRead,
    SettingsUpdate,
    TestKeyResult,
    ThinkingDisplay,
)
from app.services import anthropic_models
from app.services import settings as settings_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["settings"])


def _as_text(value: Any) -> str:
    """Settings are stored as TEXT; booleans get the spelling the parser expects."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _one_of[T: str](value: str, allowed: tuple[T, ...], key: str) -> T:
    """The stored value if it is one of ``allowed``, else that key's default.

    ``PUT /api/settings`` validates both of these fields, so the only way an
    unknown value gets in is a hand-edited database — and answering that with a
    500 from response validation would lock the user out of the Settings page
    that could fix it. The frontend keeps its own "(unknown value)" option for
    the same reason: neither side trusts the other to have coerced first.
    """
    if value in allowed:
        return value  # type: ignore[return-value]
    logger.warning("Stored %s is not a known value; falling back to the default", key)
    return settings_service.DEFAULT_SETTINGS[key]  # type: ignore[return-value]


async def _read(session: DbSession, settings: AppSettings) -> SettingsRead:
    api_key = await settings_service.get_str(session, "anthropic_api_key")
    return SettingsRead(
        model=await settings_service.get_str(session, "model"),
        effort=_one_of(
            await settings_service.get_str(session, "effort"), get_args(Effort), "effort"
        ),
        thinking_display=_one_of(
            await settings_service.get_str(session, "thinking_display"),
            get_args(ThinkingDisplay),
            "thinking_display",
        ),
        # "a key is stored in *this database*" — deliberately not "a key is
        # usable", which is what ``key_source`` answers: an ``ANTHROPIC_API_KEY``
        # from the environment or .env works without anything being stored.
        has_api_key=bool(api_key.strip()),
        api_key_masked=settings_service.mask_key(api_key),
        key_source=await settings_service.get_key_source(session, settings),
        web_search_enabled=await settings_service.get_bool(session, "web_search_enabled"),
        web_search_max_uses=await settings_service.get_int(session, "web_search_max_uses"),
        web_fetch_enabled=await settings_service.get_bool(session, "web_fetch_enabled"),
        max_tool_turns=await settings_service.get_int(session, "max_tool_turns"),
        note_template=await settings_service.get_str(session, "note_template"),
        system_prompt_extra=await settings_service.get_str(session, "system_prompt_extra"),
        feed_timeout_s=await settings_service.get_int(session, "feed_timeout_s"),
    )


@router.get("/settings", response_model=SettingsRead)
async def read_settings(session: DbSession, settings: AppSettings) -> SettingsRead:
    """Current settings, with the API key present only as a mask."""
    return await _read(session, settings)


@router.put("/settings", response_model=SettingsRead)
async def update_settings(
    update: SettingsUpdate, session: DbSession, settings: AppSettings
) -> SettingsRead:
    """Update the fields that were sent; unsent fields keep their stored values."""
    changes = {
        key: _as_text(value)
        for key, value in update.model_dump(exclude_unset=True).items()
        if value is not None
    }
    if "anthropic_api_key" in changes:
        changes["anthropic_api_key"] = changes["anthropic_api_key"].strip()

    await settings_service.set_many(session, changes)
    await session.commit()
    return await _read(session, settings)


@router.post("/settings/test-key", response_model=TestKeyResult)
async def test_api_key(client: AnthropicClient) -> TestKeyResult:
    """Check the effective key (env override or stored) against the live API."""
    return await anthropic_models.check_api_key(client)
