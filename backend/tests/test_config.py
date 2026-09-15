"""`Settings` parsing and the `python -m app` entrypoint.

Nothing here starts a server: `serve` is checked by capturing the call it makes
to uvicorn, which is the whole of its job.
"""

from __future__ import annotations

from typing import Any

import pytest

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


def test_serve_defaults_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """No authentication anywhere in this app: a dev run must not bind 0.0.0.0."""
    calls = capture_uvicorn(monkeypatch)

    entrypoint.main([])

    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["reload"] is False


def test_the_host_and_reload_flags_reach_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_uvicorn(monkeypatch)

    entrypoint.main(["--host", "0.0.0.0", "--reload"])

    assert calls[0]["host"] == "0.0.0.0"
    assert calls[0]["reload"] is True


def test_main_serves_the_process_wide_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_uvicorn(monkeypatch)
    monkeypatch.setattr(entrypoint, "settings", Settings(port=8123))

    entrypoint.main([])

    assert calls[0]["port"] == 8123
