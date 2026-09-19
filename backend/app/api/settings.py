"""Settings endpoints.

The stored Anthropic API key is write-only across this whole module: it can be set
through ``PUT /api/settings`` and is only ever read back masked.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter

from app.api.deps import AnthropicClient, DbSession
from app.kb.capture import log_activity
from app.kb.embeddings import EMBEDDING_MODELS, discard_vectors
from app.kb.schema import (
    KB_SCHEMA_VERSION_KEY,
    current_schema_version,
    parse_schema_version,
)
from app.schemas.settings import (
    CompileMode,
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


async def _read(session: DbSession) -> SettingsRead:
    api_key = await settings_service.get_str(session, "anthropic_api_key")
    voyage_key = await settings_service.get_str(session, "voyage_api_key")
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
        # The database is the only place a key can be, so "stored here" and
        # "usable" are the same statement.
        has_api_key=bool(api_key.strip()),
        api_key_masked=settings_service.mask_key(api_key),
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
        # `mask_key` invents no prefix for a Voyage key, so this is the bare
        # `…c3d4` form.
        has_voyage_key=bool(voyage_key.strip()),
        voyage_api_key_masked=settings_service.mask_key(voyage_key),
        kb_embedding_model=await settings_service.get_str(session, "kb_embedding_model"),
        # Not a preference and not stored, like `kb_compile_prompt_default`: what
        # this build can embed with, so the UI's select needs no list of its own.
        kb_embedding_models=list(EMBEDDING_MODELS),
        kb_capture_findings=await settings_service.get_bool(session, "kb_capture_findings"),
        kb_compile_mode=cast(
            CompileMode, await settings_service.get_choice(session, "kb_compile_mode")
        ),
        kb_compile_model=await settings_service.get_str(session, "kb_compile_model"),
        kb_compile_effort=cast(
            Effort, await settings_service.get_choice(session, "kb_compile_effort")
        ),
        kb_compile_prompt=await settings_service.get_str(session, "kb_compile_prompt"),
        # Not a preference and not stored: this is what the build ships with, so
        # "Reset to default" has something true to reset to.
        kb_compile_prompt_default=settings_service.DEFAULT_COMPILE_PROMPT,
        kb_compile_max_chars=await settings_service.get_int(session, "kb_compile_max_chars"),
        kb_compile_monthly_token_budget=await settings_service.get_int(
            session, "kb_compile_monthly_token_budget"
        ),
        kb_auto_accept_suggestions=await settings_service.get_bool(
            session, "kb_auto_accept_suggestions"
        ),
        kb_reviewed_only=await settings_service.get_bool(session, "kb_reviewed_only"),
        kb_recency_boost=await settings_service.get_bool(session, "kb_recency_boost"),
        kb_rerank=await settings_service.get_bool(session, "kb_rerank"),
        kb_duplicate_threshold=await settings_service.get_float(
            session, "kb_duplicate_threshold"
        ),
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
async def read_settings(session: DbSession) -> SettingsRead:
    """Current settings, with the API key present only as a mask."""
    return await _read(session)


@router.put("/settings", response_model=SettingsRead)
async def update_settings(
    update: SettingsUpdate, session: DbSession
) -> SettingsRead:
    """Update the fields that were sent; unsent fields keep their stored values."""
    changes = {
        key: _as_text(value)
        for key, value in update.model_dump(exclude_unset=True).items()
        if value is not None
    }
    for key in ("anthropic_api_key", "voyage_api_key"):
        if key in changes:
            changes[key] = changes[key].strip()

    # Read before the write: a PUT that re-sends the model it already holds is not
    # a model change, and must not throw away a perfectly good index.
    new_model = changes.get("kb_embedding_model")
    old_model = await settings_service.get_str(session, "kb_embedding_model")

    await settings_service.set_many(session, changes)
    if new_model and new_model != old_model:
        # Same transaction as the settings write: the row saying which model the
        # knowledge base embeds with and the vectors of the previous one must
        # never be true at the same time (decision C1).
        pending = await discard_vectors(session)
        await log_activity(
            session,
            "reindex",
            source="settings",
            detail=(
                f"embedding model changed from {old_model} to {new_model}; "
                f"{pending} chunks marked pending"
            ),
        )
    await session.commit()
    return await _read(session)


@router.post("/settings/test-key", response_model=TestKeyResult)
async def test_api_key(client: AnthropicClient) -> TestKeyResult:
    """Check the stored key against the live API."""
    return await anthropic_models.check_api_key(client)
