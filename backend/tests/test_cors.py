"""What the CORS middleware actually answers, per origin.

`app.config` refuses a wildcard before it can reach the middleware; these tests
are the other half — that a configured list really is a list, and that the API
never invites a browser to send credentials to it.
"""

from __future__ import annotations

import httpx2
import pytest
from httpx2 import ASGITransport

from app.config import Settings
from app.main import create_app

ALLOWED = "http://localhost:5173"


@pytest.fixture
def cors_client() -> httpx2.AsyncClient:
    app = create_app(Settings(cors_origins=[ALLOWED], _env_file=None))  # type: ignore[call-arg]
    return httpx2.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_a_listed_origin_is_allowed(cors_client: httpx2.AsyncClient) -> None:
    async with cors_client as client:
        response = await client.get("/api/health", headers={"Origin": ALLOWED})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED


async def test_an_unlisted_origin_gets_nothing(cors_client: httpx2.AsyncClient) -> None:
    """The failure this guards is a page on some other site reading the local API."""
    async with cors_client as client:
        response = await client.get("/api/health", headers={"Origin": "https://evil.example"})
        preflight = await client.options(
            "/api/settings",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "PUT",
            },
        )

    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-origin" not in preflight.headers


async def test_credentials_are_never_invited(cors_client: httpx2.AsyncClient) -> None:
    """This API has no cookies and no Authorization header, so allowing
    credentials only widens what a mistake in the origin list costs."""
    async with cors_client as client:
        response = await client.get("/api/health", headers={"Origin": ALLOWED})

    assert "access-control-allow-credentials" not in response.headers


async def test_no_cors_headers_at_all_when_nothing_is_configured() -> None:
    """The default: the SPA is served same-origin, so the middleware is absent."""
    app = create_app(Settings(_env_file=None))  # type: ignore[call-arg]
    transport = ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/health", headers={"Origin": ALLOWED})

    assert "access-control-allow-origin" not in response.headers
