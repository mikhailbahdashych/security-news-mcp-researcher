"""Settings endpoints.

The stored Anthropic API key is write-only across this whole module: it can be set
through ``PUT /api/settings`` and is only ever read back masked.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter

from app.api.deps import AnthropicClient, AppSettings, DbSession
from app.kb.schema import (
    KB_SCHEMA_VERSION_KEY,
    current_schema_version,
    parse_schema_version,
)
from app.schemas.settings import (
    Effort,
    KbSchemaVersionRead,
    SettingsRead,
    SettingsUpdate,
    TestKeyResult,
    ThinkingDisplay,
)
from app.services import anthropic_models
from app.services import settings as settings_service

router = APIRouter(tags=["settings"])


def _as_text(value: Any) -> str:
    """Settings are stored as TEXT; booleans get the spelling the parser expects."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


async def _read(session: DbSession, settings: AppSettings) -> SettingsRead:
    api_key = await settings_service.get_str(session, "anthropic_api_key")
    return SettingsRead(
        model=await settings_service.get_str(session, "model"),
        # Coerced in the service, not here, so that the turn settings read the
        # same value this page shows — see `settings_service.ALLOWED_VALUES`.
        # The frontend keeps its own "(unknown value)" option regardless:
        # neither side trusts the other to have coerced first.
        effort=cast(Effort, await settings_service.get_choice(session, "effort")),
        thinking_display=cast(
            ThinkingDisplay, await settings_service.get_choice(session, "thinking_display")
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
        kb_capture_starred=await settings_service.get_bool(session, "kb_capture_starred"),
        kb_capture_notes=await settings_service.get_bool(session, "kb_capture_notes"),
        kb_min_snapshot_chars=await settings_service.get_int(session, "kb_min_snapshot_chars"),
        kb_schema_version=await _schema_version(session),
    )


async def _schema_version(session: DbSession) -> KbSchemaVersionRead:
    """What the file records, falling back to what this build would create.

    An unreadable or absent row is not an error to surface here: ``index_status``
    already reports "no index format recorded" as an outdated index, and the
    panel still needs something to show.
    """
    stored = parse_schema_version(await settings_service.get(session, KB_SCHEMA_VERSION_KEY))
    return KbSchemaVersionRead.model_validate(current_schema_version() | stored)


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
