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
    for name in ("PORT", "DB_PATH", "LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)


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
