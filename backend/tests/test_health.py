import httpx2


async def test_health_returns_ok(client: httpx2.AsyncClient) -> None:
    response = await client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == "0.1.0"


async def test_unknown_api_route_returns_json_404(client: httpx2.AsyncClient) -> None:
    response = await client.get("/api/nonexistent")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert "detail" in response.json()
    assert "<html" not in response.text.lower()
