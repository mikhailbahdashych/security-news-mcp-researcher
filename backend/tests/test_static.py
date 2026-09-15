from pathlib import Path

import httpx2
import pytest
from httpx2 import ASGITransport

from app.config import Settings
from app.main import create_app

INDEX_HTML = '<!doctype html><html><body><div id="root"></div></body></html>'


#: Written next to `static/`, so serving it means the resolver escaped the root.
SECRET = "SUPERSECRET-token-value"


@pytest.fixture
def spa_client(tmp_path: Path) -> httpx2.AsyncClient:
    """Client for an app whose static_dir exists and holds a built SPA."""
    static_dir = tmp_path / "static"
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text(INDEX_HTML)
    (static_dir / "assets" / "app.js").write_text("console.log('hi')")
    (tmp_path / "secrets.txt").write_text(SECRET)

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
    assert SECRET not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/%2e%2e/secrets.txt",
        "/%2E%2E/secrets.txt",
        "/docs/%2e%2e/%2e%2e/secrets.txt",
        "/%2e%2e%2fsecrets.txt",
    ],
)
async def test_percent_encoded_traversal_is_not_served(
    spa_client: httpx2.AsyncClient, path: str
) -> None:
    """The plain `/../x` case is near-tautological: httpx2 normalises the dots out
    of the URL before the request is ever made, so the app never sees them. These
    reach the resolver with `..` intact — an unquoted path is what ASGI hands the
    route — which is the case `_resolve_static_file` actually has to refuse.

    Every path here must stay off `/assets`, which is a separate mount with a
    guard of its own (below) and never reaches the resolver at all."""
    async with spa_client as client:
        response = await client.get(path)

    assert response.status_code in (200, 404)
    assert SECRET not in response.text


async def test_the_assets_mount_refuses_traversal_of_its_own(
    spa_client: httpx2.AsyncClient,
) -> None:
    """`/assets` is a `StaticFiles` mount, not the SPA catch-all: it answers 404
    before `_resolve_static_file` is called. Pinned separately so that the
    resolver's own cases above are not quietly testing this instead."""
    async with spa_client as client:
        response = await client.get("/assets/%2e%2e/%2e%2e/secrets.txt")

    assert response.status_code == 404
    assert SECRET not in response.text
