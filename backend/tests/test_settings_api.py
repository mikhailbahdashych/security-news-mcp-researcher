"""`/api/settings`, `/api/settings/test-key` and the raw-key-leak guarantee."""

import logging

import anthropic
import httpx2
import pytest
from fakes.anthropic import FakeAnthropicClient, api_error
from httpx2 import ASGITransport

from app.api.deps import get_anthropic_client
from app.config import Settings
from app.kb import schema as kb_schema
from app.services import settings as settings_service

RAW_KEY = "sk-ant-api03-supersecretvalue-a1b2"
RAW_VOYAGE_KEY = "pa-voyagesupersecretvalue-c3d4"


@pytest.fixture(autouse=True)
def isolated_voyage_key_env(monkeypatch):
    """Keep an ambient ``VOYAGE_API_KEY`` out of this module.

    ``conftest.py``'s autouse fixture does this for ``ANTHROPIC_API_KEY``, but
    ``conftest.py`` has a single writer this phase — see the report's docs delta.
    """
    monkeypatch.delenv(settings_service.VOYAGE_KEY_ENV_VAR, raising=False)


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
        "key_source": "none",
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
        "voyage_key_source": "none",
        "kb_embedding_model": "voyage-4",
        "kb_capture_findings": False,
        "kb_compile_mode": "manual",
        "kb_compile_model": "claude-sonnet-5",
        "kb_compile_effort": "low",
        "kb_compile_prompt": settings_service.DEFAULT_COMPILE_PROMPT,
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

    dependency = get_anthropic_client(db_session, Settings(anthropic_api_key=""))
    client = await anext(dependency)

    assert client is not None
    assert not client.is_closed()

    with pytest.raises(StopAsyncIteration):
        await anext(dependency)

    assert client.is_closed()


async def test_anthropic_client_is_none_without_a_key(db_session) -> None:
    dependency = get_anthropic_client(db_session, Settings(anthropic_api_key=""))

    assert await anext(dependency) is None

    with pytest.raises(StopAsyncIteration):
        await anext(dependency)


# ---------------------------------- the key that lives in .env, not the database


#: Stands in for an ``ANTHROPIC_API_KEY=`` line in ``.env``: pydantic-settings puts
#: it on ``Settings``, and never into ``os.environ``.
DOTENV_KEY = "sk-ant-api03-from-the-dotenv-file-z9y8"


class ClosableFakeClient(FakeAnthropicClient):
    """``FakeAnthropicClient`` plus the ``close()`` the dependency awaits."""

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def dotenv_app(app_factory, tmp_path, monkeypatch):
    """An app whose ``Settings`` carry the key — and nothing else does.

    The stub records the key ``get_anthropic_client`` built it with, so a test can
    assert the whole path (``.env`` -> ``Settings`` -> effective key -> client)
    rather than only the endpoint's answer.
    """
    built: list[str] = []

    def fake_build(api_key: str) -> ClosableFakeClient:
        built.append(api_key)
        return ClosableFakeClient()

    monkeypatch.setattr("app.api.deps.build_anthropic_client", fake_build)
    application = app_factory(
        Settings(
            db_path=tmp_path / "app.db",
            static_dir=tmp_path / "absent",
            anthropic_api_key=DOTENV_KEY,
        )
    )
    return application, built


@pytest.fixture
async def dotenv_client(dotenv_app):
    application, built = dotenv_app
    async with httpx2.AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as http:
        yield http, built


async def test_a_key_only_in_dotenv_is_used_for_the_live_check(dotenv_client) -> None:
    """The bug this guards: ``.env.example`` advertised ``ANTHROPIC_API_KEY``, the
    service read ``os.environ``, and a user who followed the README got "no API key
    configured" with a perfectly good key on disk."""
    http, built = dotenv_client

    response = await http.post("/api/settings/test-key")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "error": None}
    assert built == [DOTENV_KEY]


async def test_a_key_only_in_dotenv_leaves_has_api_key_false_but_names_the_source(
    dotenv_client,
) -> None:
    """``has_api_key`` keeps meaning "stored here"; ``key_source`` explains the rest."""
    http, _ = dotenv_client

    body = (await http.get("/api/settings")).json()

    assert body["has_api_key"] is False
    assert body["api_key_masked"] == ""
    assert body["key_source"] == "env"
    # Still write-only: the raw key never travels, whichever source it came from.
    assert DOTENV_KEY not in str(body)


async def test_the_dotenv_key_is_never_written_to_the_database(dotenv_client) -> None:
    http, _ = dotenv_client

    await http.post("/api/settings/test-key")

    assert (await http.get("/api/settings")).json()["has_api_key"] is False


#: The other spelling of "configured outside the app": a real environment
#: variable, which wins over both ``.env`` and the database.
ENV_KEY = "sk-ant-api03-from-the-process-environment-p0q1"


@pytest.fixture
def env_key_client(app, monkeypatch):
    """The app plus ``ANTHROPIC_API_KEY`` in the process environment.

    ``isolated_api_key_env`` deletes the variable for every test, so setting it
    here is the only way it is ever present — and the stub records the key the
    dependency built a client with, which is the half of the path an assertion on
    ``key_source`` alone would miss.
    """
    built: list[str] = []

    def fake_build(api_key: str) -> ClosableFakeClient:
        built.append(api_key)
        return ClosableFakeClient()

    monkeypatch.setenv(settings_service.API_KEY_ENV_VAR, ENV_KEY)
    monkeypatch.setattr("app.api.deps.build_anthropic_client", fake_build)
    return built


async def test_the_environment_key_is_used_and_named_as_the_source(
    client: httpx2.AsyncClient, env_key_client
) -> None:
    built = env_key_client

    settings_body = (await client.get("/api/settings")).json()
    test_key = await client.post("/api/settings/test-key")

    # Nothing is stored, and the app still works — which is what `key_source` is
    # there to explain.
    assert settings_body["has_api_key"] is False
    assert settings_body["key_source"] == "env"
    assert test_key.json() == {"ok": True, "error": None}
    assert built == [ENV_KEY]
    assert ENV_KEY not in str(settings_body)


async def test_the_environment_key_beats_a_stored_one_and_is_never_written_down(
    client: httpx2.AsyncClient, env_key_client
) -> None:
    built = env_key_client
    await client.put("/api/settings", json={"anthropic_api_key": RAW_KEY})

    body = (await client.get("/api/settings")).json()
    await client.post("/api/settings/test-key")

    # The stored key is still stored — it is just not the one being used.
    assert body["has_api_key"] is True
    assert body["key_source"] == "env"
    assert built == [ENV_KEY]


async def test_key_source_is_stored_once_a_key_is_saved(client: httpx2.AsyncClient) -> None:
    assert (await client.get("/api/settings")).json()["key_source"] == "none"

    await client.put("/api/settings", json={"anthropic_api_key": RAW_KEY})

    assert (await client.get("/api/settings")).json()["key_source"] == "stored"


# ------------------------------------------------- the Voyage key and Phase 2


async def test_the_voyage_key_is_only_ever_read_back_masked(client: httpx2.AsyncClient) -> None:
    put_response = await client.put("/api/settings", json={"voyage_api_key": RAW_VOYAGE_KEY})
    get_response = await client.get("/api/settings")

    assert put_response.status_code == 200
    for response in (put_response, get_response):
        body = response.json()
        assert body["has_voyage_key"] is True
        assert body["voyage_key_source"] == "stored"
        assert body["voyage_api_key_masked"] == "…c3d4"
        assert RAW_VOYAGE_KEY not in response.text
        assert "voyagesupersecret" not in response.text

    await client.put("/api/settings", json={"voyage_api_key": ""})
    cleared = (await client.get("/api/settings")).json()
    assert cleared["has_voyage_key"] is False
    assert cleared["voyage_api_key_masked"] == ""


async def test_the_voyage_env_override_wins_over_the_stored_key(
    client: httpx2.AsyncClient, app_factory, tmp_path, monkeypatch
) -> None:
    """``has_voyage_key`` keeps meaning "stored here"; the source explains the rest."""
    monkeypatch.setenv(settings_service.VOYAGE_KEY_ENV_VAR, "pa-from-the-environment-0001")

    body = (await client.get("/api/settings")).json()
    assert body["has_voyage_key"] is False
    assert body["voyage_key_source"] == "env"

    await client.put("/api/settings", json={"voyage_api_key": RAW_VOYAGE_KEY})
    stored_too = (await client.get("/api/settings")).json()
    assert stored_too["has_voyage_key"] is True
    assert stored_too["voyage_key_source"] == "env"

    # The other spelling of "configured outside the app": a key in `.env`, which
    # reaches the app as `Settings` and never as an environment variable.
    monkeypatch.delenv(settings_service.VOYAGE_KEY_ENV_VAR, raising=False)
    dotenv_app = app_factory(
        Settings(
            db_path=tmp_path / "app.db",
            static_dir=tmp_path / "absent",
            anthropic_api_key="",
            voyage_api_key="pa-from-the-dotenv-0002",
        )
    )
    async with httpx2.AsyncClient(
        transport=ASGITransport(app=dotenv_app), base_url="http://test"
    ) as http:
        from_dotenv = (await http.get("/api/settings")).json()
    assert from_dotenv["voyage_key_source"] == "env"
    assert "pa-from-the-dotenv-0002" not in str(from_dotenv)


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
