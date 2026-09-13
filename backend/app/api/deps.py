"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from anthropic import AsyncAnthropic
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """The app's session factory, for services that open their own transactions.

    A request-scoped session is the wrong tool when a service fans out over eight
    concurrent feed fetches: each one should commit as soon as it is done rather
    than hold a single SQLite write transaction open for the whole batch. Such a
    service is handed the factory instead, through this dependency so that tests can
    override it exactly as they override ``get_db``.
    """
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise RuntimeError(
            "The database is not initialised. The application lifespan did not run — "
            "tests that drive the app through ASGITransport must override "
            "get_session_factory."
        )
    return session_factory


SessionFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


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


__all__ = [
    "AnthropicClient",
    "DbSession",
    "SessionFactory",
    "get_anthropic_client",
    "get_db",
    "get_session_factory",
]
