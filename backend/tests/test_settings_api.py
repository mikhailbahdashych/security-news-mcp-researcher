"""`/api/settings`, `/api/settings/test-key` and the raw-key-leak guarantee."""

import anthropic
import httpx2
import pytest
from fake_anthropic import FakeAnthropicClient, api_error

from app.api.deps import get_anthropic_client
from app.services import settings as settings_service

RAW_KEY = "sk-ant-api03-supersecretvalue-a1b2"


@pytest.fixture
def use_client(app):
    """Install a stub Anthropic client (or ``None``) for the endpoints that need one."""

    def install(client: object | None) -> None:
        app.dependency_overrides[get_anthropic_client] = lambda: client

    return install


async def test_get_settings_returns_seeded_defaults(client: httpx2.AsyncClient) -> None:
    response = await client.get("/api/settings")

    assert response.status_code == 200
    assert response.json() == {
        "model": "claude-opus-5",
        "effort": "high",
        "thinking_display": "summarized",
        "has_api_key": False,
        "api_key_masked": "",
        "web_search_enabled": True,
        "web_search_max_uses": 8,
        "web_fetch_enabled": True,
        "max_tool_turns": 12,
        "note_template": settings_service.DEFAULT_NOTE_TEMPLATE,
        "system_prompt_extra": "",
        "feed_timeout_s": 15,
    }


async def test_put_updates_only_the_fields_sent(client: httpx2.AsyncClient) -> None:
    response = await client.put("/api/settings", json={"effort": "low", "web_search_max_uses": 3})

    assert response.status_code == 200
    body = response.json()
    assert body["effort"] == "low"
    assert body["web_search_max_uses"] == 3
    # Everything else is untouched.
    assert body["model"] == "claude-opus-5"
    assert body["thinking_display"] == "summarized"
    assert body["max_tool_turns"] == 12

    assert (await client.get("/api/settings")).json() == body


async def test_put_persists_across_requests(client: httpx2.AsyncClient) -> None:
    await client.put("/api/settings", json={"note_template": "# Custom", "feed_timeout_s": 42})

    body = (await client.get("/api/settings")).json()
    assert body["note_template"] == "# Custom"
    assert body["feed_timeout_s"] == 42


async def test_api_key_is_stored_masked_and_never_echoed(client: httpx2.AsyncClient) -> None:
    put_response = await client.put("/api/settings", json={"anthropic_api_key": RAW_KEY})
    get_response = await client.get("/api/settings")

    assert put_response.status_code == 200
    for response in (put_response, get_response):
        body = response.json()
        assert body["has_api_key"] is True
        assert body["api_key_masked"] == "sk-ant-…a1b2"
        # The whole serialised response must not contain the raw key anywhere.
        assert RAW_KEY not in response.text
        assert "supersecret" not in response.text


async def test_clearing_the_api_key_flips_has_api_key(client: httpx2.AsyncClient) -> None:
    await client.put("/api/settings", json={"anthropic_api_key": RAW_KEY})
    assert (await client.get("/api/settings")).json()["has_api_key"] is True

    await client.put("/api/settings", json={"anthropic_api_key": ""})

    body = (await client.get("/api/settings")).json()
    assert body["has_api_key"] is False
    assert body["api_key_masked"] == ""


async def test_put_rejects_unknown_values(client: httpx2.AsyncClient) -> None:
    assert (await client.put("/api/settings", json={"effort": "turbo"})).status_code == 422
    assert (await client.put("/api/settings", json={"thinking_display": "loud"})).status_code == 422
    assert (await client.put("/api/settings", json={"web_search_max_uses": 0})).status_code == 422
    assert (await client.put("/api/settings", json={"nonsense": 1})).status_code == 422


async def test_test_key_reports_success(client: httpx2.AsyncClient, use_client) -> None:
    use_client(FakeAnthropicClient())

    response = await client.post("/api/settings/test-key")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "error": None}


async def test_test_key_reports_an_auth_failure(client: httpx2.AsyncClient, use_client) -> None:
    use_client(FakeAnthropicClient(error=api_error(401)))

    response = await client.post("/api/settings/test-key")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "key" in body["error"].lower()


@pytest.mark.parametrize("status_code", [403, 429, 500])
async def test_test_key_reports_other_api_failures(
    client: httpx2.AsyncClient, use_client, status_code: int
) -> None:
    use_client(FakeAnthropicClient(error=api_error(status_code)))

    body = (await client.post("/api/settings/test-key")).json()
    assert body["ok"] is False
    assert body["error"]


async def test_test_key_reports_a_connection_failure(
    client: httpx2.AsyncClient, use_client
) -> None:
    use_client(FakeAnthropicClient(error=anthropic.APIConnectionError(request=None)))

    body = (await client.post("/api/settings/test-key")).json()
    assert body["ok"] is False
    assert body["error"]


async def test_test_key_without_a_key(client: httpx2.AsyncClient) -> None:
    # No override: the real dependency sees an empty stored key and returns None.
    response = await client.post("/api/settings/test-key")

    assert response.status_code == 200
    assert response.json() == {"ok": False, "error": "no API key configured"}


async def test_test_key_error_never_contains_the_key(
    client: httpx2.AsyncClient, use_client
) -> None:
    await client.put("/api/settings", json={"anthropic_api_key": RAW_KEY})
    use_client(FakeAnthropicClient(error=api_error(401, message=f"invalid key {RAW_KEY}")))

    response = await client.post("/api/settings/test-key")

    assert RAW_KEY not in response.text


async def test_anthropic_client_is_closed_when_the_request_ends(db_session) -> None:
    """The client owns an httpx2 pool; leaking one per request would leak sockets."""
    await settings_service.set_value(db_session, "anthropic_api_key", RAW_KEY)

    dependency = get_anthropic_client(db_session)
    client = await anext(dependency)

    assert client is not None
    assert not client.is_closed()

    with pytest.raises(StopAsyncIteration):
        await anext(dependency)

    assert client.is_closed()


async def test_anthropic_client_is_none_without_a_key(db_session) -> None:
    dependency = get_anthropic_client(db_session)

    assert await anext(dependency) is None

    with pytest.raises(StopAsyncIteration):
        await anext(dependency)
