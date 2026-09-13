"""The model picker's options, straight from the Anthropic Models API."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import AnthropicClient, DbSession
from app.schemas.settings import ModelOption
from app.services import anthropic_models
from app.services import settings as settings_service

router = APIRouter(tags=["models"])


@router.get("/models", response_model=list[ModelOption])
async def list_models(session: DbSession, client: AnthropicClient) -> list[ModelOption]:
    """Available models, newest first; an empty list when no key is configured."""
    api_key = await settings_service.get_effective_api_key(session)
    return await anthropic_models.list_models(client, api_key)
