"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from anthropic import AsyncAnthropic
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session_factory
from app.services import settings as settings_service


async def get_db() -> AsyncIterator[AsyncSession]:
    """One database session per request.

    Nothing is committed automatically: routes commit their own writes, so a route
    that raises leaves the database untouched.
    """
    async with get_session_factory()() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db)]


async def get_anthropic_client(session: DbSession) -> AsyncAnthropic | None:
    """A client built from the effective API key, or ``None`` when none is configured.

    Returning ``None`` rather than raising keeps "no key yet" an ordinary state for
    the settings page. Tests override this dependency with a stub client.
    """
    api_key = await settings_service.get_effective_api_key(session)
    if not api_key:
        return None
    return AsyncAnthropic(api_key=api_key)


AnthropicClient = Annotated[AsyncAnthropic | None, Depends(get_anthropic_client)]


__all__ = ["AnthropicClient", "DbSession", "get_anthropic_client", "get_db"]
