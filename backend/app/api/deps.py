"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Annotated

from anthropic import AsyncAnthropic
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.mcp.manager import McpManager
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


def get_mcp_manager(request: Request) -> McpManager:
    """The app's MCP manager, created by ``create_app`` and closed by the lifespan.

    It is built in ``create_app`` rather than in the lifespan because it holds no
    resources until something asks it for a tool — creating it early costs nothing
    and means the test suite, which does not run the lifespan, still has one.
    """
    manager = getattr(request.app.state, "mcp_manager", None)
    if manager is None:  # pragma: no cover - create_app always sets it
        raise RuntimeError("The MCP manager is missing from app.state.")
    return manager


McpManagerDep = Annotated[McpManager, Depends(get_mcp_manager)]


def get_app_settings(request: Request) -> Settings:
    """This app's :class:`Settings`, put on ``app.state`` by ``create_app``.

    Reached through a dependency rather than the module-level ``app.config.settings``
    so that an app built with ``create_app(Settings(...))`` — every test, and any
    second app in one process — really does get its own values. The only field
    routes read from it today is ``anthropic_api_key`` (``.env``'s key).
    """
    settings = getattr(request.app.state, "settings", None)
    if settings is None:  # pragma: no cover - create_app always sets it
        raise RuntimeError("Settings are missing from app.state.")
    return settings


AppSettings = Annotated[Settings, Depends(get_app_settings)]


def build_anthropic_client(api_key: str) -> AsyncAnthropic:
    """The one place a real ``AsyncAnthropic`` is constructed — and the one seam a
    test monkeypatches to keep a request off the network."""
    return AsyncAnthropic(api_key=api_key)


async def get_anthropic_client(
    session: DbSession, settings: AppSettings
) -> AsyncIterator[AsyncAnthropic | None]:
    """A client built from the effective API key, or ``None`` when none is configured.

    The client owns an httpx2 connection pool, so it is closed when the request ends;
    a client per request rather than a shared one keeps that lifetime unambiguous,
    which matters once a streaming chat turn holds a connection open for minutes and
    the key can change underneath it.

    Returning ``None`` rather than raising keeps "no key yet" an ordinary state for
    the settings page. Tests override this dependency with a stub client.
    """
    api_key = await settings_service.get_effective_api_key(session, settings)
    if not api_key:
        yield None
        return

    # Through the same factory the streaming routes use, so there is exactly one
    # place in the app that constructs a real client — and one seam for tests.
    client = build_anthropic_client(api_key)
    try:
        yield client
    finally:
        await client.close()


AnthropicClient = Annotated[AsyncAnthropic | None, Depends(get_anthropic_client)]


def get_chat_client_factory() -> Callable[[str], AsyncAnthropic]:
    """How a *streaming* route gets its client.

    ``get_anthropic_client`` is the wrong tool for a streamed turn: FastAPI closes
    a yield-dependency when the route function returns, which for a streaming
    response is *before* the body has been sent — the pool would go away
    underneath the open stream. A streaming route therefore builds its own client
    from this factory and closes it itself when the stream finalises. Tests
    override this dependency to hand back a scripted fake.
    """
    return build_anthropic_client


ChatClientFactory = Annotated[Callable[[str], AsyncAnthropic], Depends(get_chat_client_factory)]


__all__ = [
    "AnthropicClient",
    "AppSettings",
    "ChatClientFactory",
    "DbSession",
    "McpManagerDep",
    "SessionFactory",
    "build_anthropic_client",
    "get_anthropic_client",
    "get_app_settings",
    "get_chat_client_factory",
    "get_db",
    "get_mcp_manager",
    "get_session_factory",
]
