from collections.abc import AsyncIterator

import httpx2
import pytest
from httpx2 import ASGITransport

from app.main import app


@pytest.fixture
async def client() -> AsyncIterator[httpx2.AsyncClient]:
    """Async HTTP client wired straight into the ASGI app (no network, no server)."""
    async with httpx2.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client
