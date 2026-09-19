"""`/api/settings`, `/api/settings/test-key` and the raw-key-leak guarantee."""

import logging

import anthropic
import httpx2
import pytest
from fakes.anthropic import FakeAnthropicClient, api_error

from app.api.deps import get_anthropic_client
from app.kb import schema as kb_schema
from app.services import settings as settings_service

RAW_KEY = "sk-ant-api03-supersecretvalue-a1b2"
RAW_VOYAGE_KEY = "pa-voyagesupersecretvalue-c3d4"


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
        "kb_capture_starred": True,
        "kb_capture_notes": True,
        "kb_min_snapshot_chars": 400,
        "kb_schema_version": kb_schema.current_schema_version(),
        "has_voyage_key": False,
        "voyage_api_key_masked": "",
        "kb_embedding_model": "voyage-4",
        "kb_embedding_models": ["voyage-4", "voyage-4-lite", "voyage-4-large"],
        "kb_capture_findings": False,
        "kb_compile_mode": "manual",
        "kb_compile_model": "claude-sonnet-5",
        "kb_compile_effort": "low",
        "kb_compile_prompt": settings_service.DEFAULT_COMPILE_PROMPT,
        "kb_compile_prompt_default": settings_service.DEFAULT_COMPILE_PROMPT,
        "kb_compile_max_chars": 24_000,
        "kb_compile_monthly_token_budget": 5_000_000,
        "kb_auto_accept_suggestions": True,
        "kb_reviewed_only": False,
        "kb_recency_boost": True,
        "kb_rerank": True,
        "kb_duplicate_threshold": 0.92,
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


async def test_test_key_survives_an_api_error_that_is_neither_status_nor_connection(
    client: httpx2.AsyncClient, use_client
) -> None:
    """``APIResponseValidationError`` and friends are ``APIError`` and nothing
    else: without the final clause they escaped as a 500 that told the user
    nothing about their key."""
    request = httpx2.Request("GET", "https://api.anthropic.com/v1/models")
    use_client(FakeAnthropicClient(error=anthropic.APIError("malformed", request, body=None)))

    response = await client.post("/api/settings/test-key")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error"]
    # Still never the SDK's own message.
    assert "malformed" not in body["error"]


# ------------------------------------------ values the database should not hold


@pytest.mark.parametrize(
    ("key", "stored", "expected"),
    [("effort", "turbo", "high"), ("thinking_display", "loud", "summarized")],
)
async def test_an_off_union_stored_value_falls_back_to_the_default(
    client: httpx2.AsyncClient, db_session, caplog, key: str, stored: str, expected: str
) -> None:
    """Both fields are closed sets in the response model, and the store behind
    them is TEXT. A hand-edited row must not turn the Settings page — the one
    place that could fix it — into a 500."""
    await settings_service.set_value(db_session, key, stored)
    await db_session.commit()
    # Guards the guard: without this the assertion below would pass just as well
    # on a write that never landed, since the fallback *is* the seeded value.
    assert await settings_service.get(db_session, key) == stored

    with caplog.at_level(logging.WARNING, logger="app.services.settings"):
        response = await client.get("/api/settings")

    assert response.status_code == 200
    assert response.json()[key] == expected
    # A coercion the user cannot see in the response has to be visible somewhere.
    assert key in caplog.text
    assert "falling back" in caplog.text
    # Reading is not repairing: the row is left for the next PUT to overwrite.
    assert await settings_service.get(db_session, key) == stored


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


# ------------------------------- the database is the only place a key can be


#: A key in the process environment. It used to win over everything; now it is
#: ignored, which is the property this section exists to hold.
ENV_KEY = "sk-ant-api03-from-the-process-environment-p0q1"


class ClosableFakeClient(FakeAnthropicClient):
    """``FakeAnthropicClient`` plus the ``close()`` the dependency awaits."""

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def built_keys(monkeypatch):
    """Records the key ``get_anthropic_client`` actually built a client with."""
    built: list[str] = []

    def fake_build(api_key: str) -> ClosableFakeClient:
        built.append(api_key)
        return ClosableFakeClient()

    monkeypatch.setattr("app.api.deps.build_anthropic_client", fake_build)
    return built


async def test_an_environment_key_is_ignored_entirely(
    client: httpx2.AsyncClient, built_keys, monkeypatch
) -> None:
    """An ``ANTHROPIC_API_KEY`` left in a shell must not spend anything.

    It used to override the stored key, which meant the Settings page could show
    one key while the app billed another. The database row is now the only source.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", ENV_KEY)

    body = (await client.get("/api/settings")).json()
    test_key = await client.post("/api/settings/test-key")

    assert body["has_api_key"] is False
    assert test_key.json() == {"ok": False, "error": "no API key configured"}
    assert built_keys == []


async def test_the_stored_key_is_the_one_the_client_is_built_with(
    client: httpx2.AsyncClient, built_keys, monkeypatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", ENV_KEY)
    await client.put("/api/settings", json={"anthropic_api_key": RAW_KEY})

    body = (await client.get("/api/settings")).json()
    await client.post("/api/settings/test-key")

    assert body["has_api_key"] is True
    assert built_keys == [RAW_KEY]
    # Still write-only, whatever else is in the environment.
    assert RAW_KEY not in str(body)
    assert ENV_KEY not in str(body)


# ------------------------------------------------- the Voyage key and Phase 2


async def test_the_voyage_key_is_only_ever_read_back_masked(client: httpx2.AsyncClient) -> None:
    put_response = await client.put("/api/settings", json={"voyage_api_key": RAW_VOYAGE_KEY})
    get_response = await client.get("/api/settings")

    assert put_response.status_code == 200
    for response in (put_response, get_response):
        body = response.json()
        assert body["has_voyage_key"] is True
        assert body["voyage_api_key_masked"] == "…c3d4"
        assert RAW_VOYAGE_KEY not in response.text
        assert "voyagesupersecret" not in response.text

    await client.put("/api/settings", json={"voyage_api_key": ""})
    cleared = (await client.get("/api/settings")).json()
    assert cleared["has_voyage_key"] is False
    assert cleared["voyage_api_key_masked"] == ""


async def test_a_voyage_key_in_the_environment_is_ignored(
    client: httpx2.AsyncClient, monkeypatch
) -> None:
    """Same single source as the Anthropic key, and the same guard."""
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-from-the-environment-0001")

    body = (await client.get("/api/settings")).json()

    assert body["has_voyage_key"] is False


PHASE_TWO_SETTINGS = {
    "kb_embedding_model": "voyage-4-lite",
    "kb_capture_findings": True,
    "kb_compile_mode": "auto",
    "kb_compile_model": "claude-opus-5",
    "kb_compile_effort": "medium",
    "kb_compile_prompt": "# Summarise it",
    "kb_compile_max_chars": 32_000,
    "kb_compile_monthly_token_budget": 250_000,
    "kb_auto_accept_suggestions": False,
    "kb_reviewed_only": True,
    "kb_recency_boost": False,
    "kb_rerank": False,
    "kb_duplicate_threshold": 0.75,
}


async def test_every_phase_two_setting_round_trips(client: httpx2.AsyncClient) -> None:
    response = await client.put("/api/settings", json=PHASE_TWO_SETTINGS)

    assert response.status_code == 200, response.text
    for body in (response.json(), (await client.get("/api/settings")).json()):
        for key, value in PHASE_TWO_SETTINGS.items():
            assert body[key] == value, key
            assert isinstance(body[key], type(value)), key


@pytest.mark.parametrize(
    "payload",
    [
        {"kb_compile_mode": "sometimes"},
        {"kb_compile_effort": "turbo"},
        {"kb_duplicate_threshold": 1.5},
        {"kb_compile_max_chars": 10},
        {"kb_embedding_model": ""},
        {"kb_compile_monthly_token_budget": -1},
    ],
)
async def test_put_rejects_out_of_range_phase_two_values(
    client: httpx2.AsyncClient, payload: dict
) -> None:
    assert (await client.put("/api/settings", json=payload)).status_code == 422


async def test_put_refuses_an_embedding_model_the_embedder_does_not_know(
    client: httpx2.AsyncClient,
) -> None:
    """A typo used to be stored, which made ``update_settings`` see a model change
    and empty the vector index — after which Embed now 502s on Voyage's 400 and
    recovery costs a full paid re-embed. The 422 lands before anything is wiped."""
    response = await client.put("/api/settings", json={"kb_embedding_model": "voyage-3.5"})

    assert response.status_code == 422
    assert "voyage-4" in str(response.json()["detail"])
    body = (await client.get("/api/settings")).json()
    assert body["kb_embedding_model"] == "voyage-4"


async def test_the_allowed_embedding_models_are_on_the_wire_and_come_from_the_embedder(
    client: httpx2.AsyncClient,
) -> None:
    """The UI renders a select from this list rather than hard-coding one, so the
    embedder's own table stays the single source of what is selectable."""
    from app.kb import embeddings

    body = (await client.get("/api/settings")).json()

    assert body["kb_embedding_models"] == list(embeddings.EMBEDDING_MODELS)
    assert body["kb_embedding_model"] in body["kb_embedding_models"]


async def test_a_hand_edited_embedding_model_is_reported_rather_than_hidden(
    client: httpx2.AsyncClient, db_session
) -> None:
    """A value written straight into SQLite (or left by a newer build) must not
    break the page: it is read back verbatim, next to the list of what is
    accepted, so the user can see what is there and pick something valid."""
    await settings_service.set_many(db_session, {"kb_embedding_model": "voyage-3.5"})
    await db_session.commit()

    body = (await client.get("/api/settings")).json()

    assert body["kb_embedding_model"] == "voyage-3.5"
    assert "voyage-3.5" not in body["kb_embedding_models"]


@pytest.mark.parametrize("blank", ["", "   ", "\n\n", "\t"])
async def test_put_refuses_to_empty_the_compile_prompt(
    client: httpx2.AsyncClient, blank: str
) -> None:
    # It used to be stored, and the getter falls back to the shipped prompt only
    # when the *row is absent* — so one save with an empty textarea destroyed the
    # default for good and every compile afterwards went out with no
    # instructions. A whitespace-only prompt is the same thing typed slower.
    assert (await client.put("/api/settings", json={"kb_compile_prompt": blank})).status_code == 422
    body = (await client.get("/api/settings")).json()
    assert body["kb_compile_prompt"] == settings_service.DEFAULT_COMPILE_PROMPT


async def test_the_shipped_compile_prompt_is_readable_beside_the_stored_one(
    client: httpx2.AsyncClient,
) -> None:
    await client.put("/api/settings", json={"kb_compile_prompt": "# Mine"})

    body = (await client.get("/api/settings")).json()

    assert body["kb_compile_prompt"] == "# Mine"
    # What "Reset to default" puts back. It is on the wire nowhere else.
    assert body["kb_compile_prompt_default"] == settings_service.DEFAULT_COMPILE_PROMPT


async def test_the_shipped_compile_prompt_is_not_writable(client: httpx2.AsyncClient) -> None:
    response = await client.put("/api/settings", json={"kb_compile_prompt_default": "# No"})

    assert response.status_code == 422
