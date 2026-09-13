from collections.abc import AsyncIterator

import httpx2
import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.deps import get_db
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
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session on the test database, for exercising services directly."""
    async with create_session_factory(db_engine)() as session:
        yield session


@pytest.fixture
def app(db_engine: AsyncEngine) -> FastAPI:
    """The application wired to the test database.

    ``ASGITransport`` does not run the lifespan, so the schema is created by the
    ``db_engine`` fixture rather than by the app's startup hook.
    """
    application = create_app()
    session_factory = create_session_factory(db_engine)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_db] = override_get_db
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    """Async HTTP client wired straight into the ASGI app (no network, no server)."""
    async with httpx2.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client
