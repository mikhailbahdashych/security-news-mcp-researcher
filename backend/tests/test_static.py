from pathlib import Path

import httpx2
import pytest
from httpx2 import ASGITransport

from app.config import Settings
from app.main import create_app

INDEX_HTML = '<!doctype html><html><body><div id="root"></div></body></html>'


@pytest.fixture
def spa_client(tmp_path: Path) -> httpx2.AsyncClient:
    """Client for an app whose static_dir exists and holds a built SPA."""
    static_dir = tmp_path / "static"
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text(INDEX_HTML)
    (static_dir / "assets" / "app.js").write_text("console.log('hi')")

    app = create_app(Settings(static_dir=static_dir))
    return httpx2.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_root_serves_index_html(spa_client: httpx2.AsyncClient) -> None:
    async with spa_client as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert 'id="root"' in response.text


async def test_client_side_route_serves_index_html(spa_client: httpx2.AsyncClient) -> None:
    async with spa_client as client:
        response = await client.get("/settings")

    assert response.status_code == 200
    assert 'id="root"' in response.text


async def test_asset_is_served_from_static_dir(spa_client: httpx2.AsyncClient) -> None:
    async with spa_client as client:
        response = await client.get("/assets/app.js")

    assert response.status_code == 200
    assert "console.log" in response.text


async def test_unknown_api_route_never_falls_through_to_index_html(
    spa_client: httpx2.AsyncClient,
) -> None:
    async with spa_client as client:
        response = await client.get("/api/nope")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "Not Found"}
    assert "<html" not in response.text.lower()


async def test_path_traversal_is_not_served(spa_client: httpx2.AsyncClient) -> None:
    async with spa_client as client:
        response = await client.get("/../secrets.txt")

    # Falls back to the SPA shell rather than escaping static_dir.
    assert response.status_code in (200, 404)
    assert "secret" not in response.text.lower()
