"""`/api/models`: mapping, ordering, the empty-key case and the one-hour cache."""

import httpx2
import pytest
from fake_anthropic import FakeAnthropicClient, api_error, model

from app.api.deps import get_anthropic_client
from app.services import anthropic_models

MODELS = [
    model("claude-sonnet-5", "Claude Sonnet 5", 2025),
    model("claude-opus-5", "Claude Opus 5", 2026),
    model("claude-haiku-4-5", "Claude Haiku 4.5", 2024),
]


@pytest.fixture(autouse=True)
def _clear_cache():
    anthropic_models.model_cache.invalidate()
    yield
    anthropic_models.model_cache.invalidate()


@pytest.fixture
def use_client(app):
    def install(client: object | None) -> None:
        app.dependency_overrides[get_anthropic_client] = lambda: client

    return install


async def test_models_are_mapped_and_sorted_newest_first(
    client: httpx2.AsyncClient, use_client
) -> None:
    use_client(FakeAnthropicClient(MODELS))

    response = await client.get("/api/models")

    assert response.status_code == 200
    assert response.json() == [
        {"id": "claude-opus-5", "display_name": "Claude Opus 5"},
        {"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5"},
        {"id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5"},
    ]


async def test_no_key_returns_an_empty_list(client: httpx2.AsyncClient) -> None:
    # No override: the real dependency sees an empty stored key and returns None.
    response = await client.get("/api/models")

    assert response.status_code == 200
    assert response.json() == []


async def test_result_is_cached_within_the_ttl(client: httpx2.AsyncClient, use_client) -> None:
    fake = FakeAnthropicClient(MODELS)
    use_client(fake)

    first = await client.get("/api/models")
    second = await client.get("/api/models")

    assert first.json() == second.json()
    assert fake.call_count == 1


async def test_cache_refetches_once_the_ttl_has_passed() -> None:
    fake = FakeAnthropicClient(MODELS)
    cache = anthropic_models.ModelCache(ttl_seconds=0.0)

    await cache.get("sk-ant-key", fake)
    await cache.get("sk-ant-key", fake)

    assert fake.call_count == 2


def test_default_cache_ttl_is_one_hour() -> None:
    assert anthropic_models.MODEL_CACHE_TTL_SECONDS == 3600.0


async def test_cache_is_keyed_on_the_api_key(client: httpx2.AsyncClient, use_client) -> None:
    fake = FakeAnthropicClient(MODELS)
    use_client(fake)

    await client.get("/api/models")
    assert fake.call_count == 1

    # Changing the stored key must not serve the previous key's model list.
    await client.put("/api/settings", json={"anthropic_api_key": "sk-ant-other-key-9999"})
    await client.get("/api/models")

    assert fake.call_count == 2


async def test_api_failure_is_not_cached_and_returns_an_empty_list(
    client: httpx2.AsyncClient, use_client
) -> None:
    fake = FakeAnthropicClient(error=api_error(401))
    use_client(fake)

    assert (await client.get("/api/models")).json() == []
    assert (await client.get("/api/models")).json() == []
    assert fake.call_count == 2
