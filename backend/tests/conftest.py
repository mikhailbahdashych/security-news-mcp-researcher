import socket
from collections.abc import AsyncIterator

import httpx2
import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.deps import get_db, get_session_factory
from app.config import Settings
from app.db.engine import create_db_engine, create_session_factory
from app.db.init import init_db
from app.main import create_app
from app.services import settings as settings_service


@pytest.fixture(autouse=True)
def isolated_api_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep an ambient ``ANTHROPIC_API_KEY`` out of the suite.

    Without this, a developer who exports a real key would have the "no key
    configured" tests quietly make live API calls. Tests that want the override
    set it themselves.
    """
    monkeypatch.delenv(settings_service.API_KEY_ENV_VAR, raising=False)
    # The Voyage key has the same precedence and the same trap: with a real one
    # in the environment, every capture in the suite would try to embed.
    monkeypatch.delenv(settings_service.VOYAGE_KEY_ENV_VAR, raising=False)


#: A public address (example.com's). The stub resolver below hands it out so that
#: fixture hostnames like ``example.test`` pass the outbound-URL guard.
PUBLIC_TEST_ADDRESS = "93.184.216.34"


@pytest.fixture(autouse=True)
def offline_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every hostname to a public address, without touching the network.

    The URL guard resolves hosts before fetching them, and the suite's fixture hosts
    (``example.test``, ``atom.example.test``, ...) do not exist. Autouse so that no
    test can reach a real resolver by accident; the guard's own tests override this
    with whatever answer they need.
    """

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_TEST_ADDRESS, port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@pytest.fixture
async def db_engine(tmp_path) -> AsyncIterator[AsyncEngine]:
    """A fresh, initialised database in a temp file — one per test.

    A real file rather than ``:memory:`` so that WAL mode and the foreign-key
    pragmas behave exactly as they do in production.
    """
    engine = create_db_engine(tmp_path / "app.db")
    await init_db(engine, create_session_factory(engine))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """The session factory services take when they open their own transactions.

    ``refresh_feeds`` keeps one short transaction per feed rather than holding a
    request-scoped session open across every fetch, so it is handed the factory the
    way the route hands it ``request.app.state.session_factory``.
    """
    return create_session_factory(db_engine)


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session on the test database, for exercising services directly."""
    async with create_session_factory(db_engine)() as session:
        yield session


@pytest.fixture
def app_factory(db_engine: AsyncEngine):
    """Build an app around given ``Settings``, wired to the test database.

    ``ASGITransport`` does not run the lifespan, so the schema is created by the
    ``db_engine`` fixture and ``get_db`` is overridden to use it. Tests that need
    an app with different settings — the ``.env`` API key, a log level — build one
    here rather than re-deriving the overrides.
    """

    def build(settings: Settings) -> FastAPI:
        application = create_app(settings)
        session_factory = create_session_factory(db_engine)

        async def override_get_db() -> AsyncIterator[AsyncSession]:
            async with session_factory() as session:
                yield session

        application.dependency_overrides[get_db] = override_get_db
        application.dependency_overrides[get_session_factory] = lambda: session_factory
        return application

    return build


@pytest.fixture
def app(app_factory, tmp_path) -> FastAPI:
    """The application wired to the test database.

    The app's own settings name the same database file so the two cannot drift
    apart, and ``anthropic_api_key`` is pinned empty for the same reason
    ``isolated_api_key_env`` deletes the environment variable: ``Settings`` reads
    ``.env``, so a developer with a real key in theirs would otherwise turn every
    "no key configured" test into a live API call.
    """
    return app_factory(
        Settings(
            db_path=tmp_path / "app.db",
            static_dir=tmp_path / "absent",
            anthropic_api_key="",
            voyage_api_key="",
        )
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    """Async HTTP client wired straight into the ASGI app (no network, no server)."""
    async with httpx2.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client
