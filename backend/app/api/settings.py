"""Settings endpoints.

The stored Anthropic API key is write-only across this whole module: it can be set
through ``PUT /api/settings`` and is only ever read back masked.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.deps import AnthropicClient, DbSession
from app.schemas.settings import SettingsRead, SettingsUpdate, TestKeyResult
from app.services import anthropic_models
from app.services import settings as settings_service

router = APIRouter(tags=["settings"])


def _as_text(value: Any) -> str:
    """Settings are stored as TEXT; booleans get the spelling the parser expects."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


async def _read(session: DbSession) -> SettingsRead:
    api_key = await settings_service.get_str(session, "anthropic_api_key")
    return SettingsRead(
        model=await settings_service.get_str(session, "model"),
        effort=await settings_service.get_str(session, "effort"),
        thinking_display=await settings_service.get_str(session, "thinking_display"),
        has_api_key=bool(api_key.strip()),
        api_key_masked=settings_service.mask_key(api_key),
        web_search_enabled=await settings_service.get_bool(session, "web_search_enabled"),
        web_search_max_uses=await settings_service.get_int(session, "web_search_max_uses"),
        web_fetch_enabled=await settings_service.get_bool(session, "web_fetch_enabled"),
        max_tool_turns=await settings_service.get_int(session, "max_tool_turns"),
        note_template=await settings_service.get_str(session, "note_template"),
        system_prompt_extra=await settings_service.get_str(session, "system_prompt_extra"),
        feed_timeout_s=await settings_service.get_int(session, "feed_timeout_s"),
    )


@router.get("/settings", response_model=SettingsRead)
async def read_settings(session: DbSession) -> SettingsRead:
    """Current settings, with the API key present only as a mask."""
    return await _read(session)


@router.put("/settings", response_model=SettingsRead)
async def update_settings(update: SettingsUpdate, session: DbSession) -> SettingsRead:
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
    return await _read(session)


@router.post("/settings/test-key", response_model=TestKeyResult)
async def test_api_key(client: AnthropicClient) -> TestKeyResult:
    """Check the effective key (env override or stored) against the live API."""
    return await anthropic_models.check_api_key(client)
