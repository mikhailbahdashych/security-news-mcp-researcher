"""`Settings` parsing and the `python -m app` entrypoint.

Nothing here starts a server: `serve` is checked by capturing the call it makes
to uvicorn, which is the whole of its job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app import __main__ as entrypoint
from app.config import Settings


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own environment must not decide these assertions.

    The ``.env`` half is handled by ``_env_file=None`` on every ``Settings`` built
    here — the file is read relative to the working directory, so a developer with
    a repo-root ``.env`` would otherwise be testing their own values.
    """
    for name in ("CORS_ORIGINS", "PORT", "DB_PATH", "STATIC_DIR", "LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)


def settings_from_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


# ------------------------------------------------------------------ CORS_ORIGINS


def test_cors_origins_accepts_a_plain_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug this guards: a plain value raised ``SettingsError`` at import time,
    so the app died with a JSON stack trace before it had logging."""
    settings = settings_from_env(monkeypatch, CORS_ORIGINS="http://localhost:5173")

    assert settings.cors_origins == ["http://localhost:5173"]


def test_cors_origins_accepts_a_comma_separated_list(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = settings_from_env(
        monkeypatch, CORS_ORIGINS="http://localhost:5173, http://127.0.0.1:5173 ,"
    )

    assert settings.cors_origins == ["http://localhost:5173", "http://127.0.0.1:5173"]


def test_cors_origins_still_accepts_the_json_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``.env`` written against the old behaviour keeps working."""
    settings = settings_from_env(monkeypatch, CORS_ORIGINS='["http://a.test", "http://b.test"]')

    assert settings.cors_origins == ["http://a.test", "http://b.test"]


def test_cors_origins_defaults_to_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(_env_file=None).cors_origins == []  # type: ignore[call-arg]
    assert settings_from_env(monkeypatch, CORS_ORIGINS="   ").cors_origins == []


def test_cors_origins_can_still_be_passed_directly() -> None:
    settings = Settings(cors_origins=["http://x.test"], _env_file=None)  # type: ignore[call-arg]

    assert settings.cors_origins == ["http://x.test"]


def test_cors_origins_reads_the_dot_env_file(tmp_path: Path) -> None:
    """The documented way to set this is `.env`, not the environment."""
    env_file = tmp_path / ".env"
    env_file.write_text("CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173\n")

    settings = Settings(_env_file=str(env_file))  # type: ignore[call-arg]

    assert settings.cors_origins == ["http://localhost:5173", "http://127.0.0.1:5173"]


def test_the_wildcard_origin_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """`*` would hand this whole API — settings, sessions, the stored key — to any
    page the user happens to have open. There is no authentication behind it."""
    with pytest.raises(ValidationError) as caught:
        settings_from_env(monkeypatch, CORS_ORIGINS="*")

    message = str(caught.value)
    assert "CORS_ORIGINS" in message
    assert "authentication" in message


def test_the_wildcard_is_refused_in_every_spelling(monkeypatch: pytest.MonkeyPatch) -> None:
    """A list is not a loophole: the JSON form, a comma list and a value passed
    straight to the constructor all go through the same validator."""
    with pytest.raises(ValidationError):
        settings_from_env(monkeypatch, CORS_ORIGINS='["http://a.test", "*"]')
    with pytest.raises(ValidationError):
        settings_from_env(monkeypatch, CORS_ORIGINS="http://a.test,*")
    with pytest.raises(ValidationError):
        Settings(cors_origins=["*"], _env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "value",
    [
        "localhost:5173",  # no scheme: never matches an Origin header
        "http://a.test/app",  # an origin has no path
        "file://",  # no host, and not a scheme a browser sends
    ],
)
def test_a_value_that_is_not_an_origin_is_refused(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """An `Origin` header is scheme://host[:port] and nothing else, and
    `CORSMiddleware` compares it by exact string — so anything else here is a
    rule that can never fire, silently."""
    with pytest.raises(ValidationError) as caught:
        settings_from_env(monkeypatch, CORS_ORIGINS=value)

    assert "CORS_ORIGINS" in str(caught.value)


def test_a_bracketed_value_that_is_not_json_says_what_is_wrong(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`CORS_ORIGINS=[http://a.test]` is a plausible "I will write a list"
    spelling. It used to escape the validator as a raw `JSONDecodeError`; now it
    falls through to the comma form and is refused by name."""
    with pytest.raises(ValidationError) as caught:
        settings_from_env(monkeypatch, CORS_ORIGINS="[http://a.test]")

    message = str(caught.value)
    assert "CORS_ORIGINS" in message
    assert "Expecting value" not in message


# --------------------------------------------------------------------- the PORT


def capture_uvicorn(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        entrypoint.uvicorn, "run", lambda target, **kwargs: calls.append({"app": target, **kwargs})
    )
    return calls


def test_serve_binds_the_configured_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """``PORT`` was a dead knob: the Makefile and the image both hard-coded 8000."""
    calls = capture_uvicorn(monkeypatch)

    entrypoint.serve(app_settings=Settings(port=9123))

    assert calls == [{"app": "app.main:app", "host": "127.0.0.1", "port": 9123, "reload": False}]


def test_serve_always_binds_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """No authentication anywhere in this app: it must never bind 0.0.0.0.

    There is no flag for this any more — the only caller that wanted one was the
    container's ``CMD``.
    """
    calls = capture_uvicorn(monkeypatch)

    entrypoint.main([])

    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["reload"] is False


def test_the_reload_flag_reaches_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_uvicorn(monkeypatch)

    entrypoint.main(["--reload"])

    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["reload"] is True


def test_main_serves_the_process_wide_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_uvicorn(monkeypatch)
    monkeypatch.setattr(entrypoint, "settings", Settings(port=8123))

    entrypoint.main([])

    assert calls[0]["port"] == 8123
