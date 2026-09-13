"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from anthropic import AsyncAnthropic
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import settings as settings_service


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """One database session per request, from the session factory this app owns.

    The factory is put on ``app.state`` by the lifespan, so an app built with
    ``create_app(Settings(db_path=...))`` really does talk to that database.

    Nothing is committed automatically: routes commit their own writes, so a route
    that raises leaves the database untouched.
    """
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise RuntimeError(
            "The database is not initialised. The application lifespan did not run — "
            "tests that drive the app through ASGITransport must override get_db."
        )
    async with session_factory() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db)]


async def get_anthropic_client(session: DbSession) -> AsyncIterator[AsyncAnthropic | None]:
    """A client built from the effective API key, or ``None`` when none is configured.

    The client owns an httpx2 connection pool, so it is closed when the request ends;
    a client per request rather than a shared one keeps that lifetime unambiguous,
    which matters once a streaming chat turn holds a connection open for minutes and
    the key can change underneath it.

    Returning ``None`` rather than raising keeps "no key yet" an ordinary state for
    the settings page. Tests override this dependency with a stub client.
    """
    api_key = await settings_service.get_effective_api_key(session)
    if not api_key:
        yield None
        return

    client = AsyncAnthropic(api_key=api_key)
    try:
        yield client
    finally:
        await client.close()


AnthropicClient = Annotated[AsyncAnthropic | None, Depends(get_anthropic_client)]


__all__ = ["AnthropicClient", "DbSession", "get_anthropic_client", "get_db"]
