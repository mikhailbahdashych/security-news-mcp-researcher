"""`Settings` and the `python -m app` entrypoint.

Nothing here starts a server: `serve` is checked by capturing the call it makes
to uvicorn, and the flags are checked by building a fresh ``Settings`` afterwards
— which is exactly what the reloader's worker process does.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from app import __main__ as entrypoint
from app.config import Settings


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A private copy of the environment, restored on teardown.

    ``serve`` writes the ``SNR_*`` hand-off variables directly, which ``monkeypatch``
    cannot undo — so the mapping itself is swapped for a copy.
    """
    monkeypatch.setattr(os, "environ", dict(os.environ))


def capture_uvicorn(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        entrypoint.uvicorn, "run", lambda target, **kwargs: calls.append({"app": target, **kwargs})
    )
    return calls


# ------------------------------------------------------------------- the flags


def test_the_flags_reach_a_settings_built_in_another_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the hand-off.

    ``uvicorn.run(..., reload=True)`` re-imports ``app.main`` — and therefore
    ``app.config`` — in a worker process, so a ``Settings`` built inside ``main()``
    never arrives. Building a fresh one here is what that worker does.
    """
    capture_uvicorn(monkeypatch)

    entrypoint.main(["--port", "8012", "--db-path", "/tmp/other.db", "--log-level", "DEBUG"])

    worker_settings = Settings()
    assert worker_settings.db_path == Path("/tmp/other.db")
    assert worker_settings.log_level == "DEBUG"


def test_the_flags_reach_the_app_built_in_this_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``--reload`` there is no worker: uvicorn imports ``app.main`` right here.

    A ``Settings`` singleton built when ``app.config`` was first imported — which
    is before ``main()`` has parsed anything — would make ``--db-path`` and
    ``--log-level`` silently do nothing, and the app would open the default
    database. ``create_app()`` has to read the hand-off when it is called.
    """
    from app.main import create_app

    capture_uvicorn(monkeypatch)

    entrypoint.main(["--db-path", "/tmp/other.db", "--log-level", "DEBUG"])

    served = create_app().state.settings
    assert served.db_path == Path("/tmp/other.db")
    assert served.log_level == "DEBUG"


def test_the_defaults_are_the_dev_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    capture_uvicorn(monkeypatch)

    entrypoint.main([])

    worker_settings = Settings()
    assert worker_settings.port == 8000
    assert worker_settings.db_path == Path("./data/app.db")
    assert worker_settings.log_level == "INFO"


def test_the_port_flag_also_reaches_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    """uvicorn binds the port itself; it does not read ``Settings``."""
    calls = capture_uvicorn(monkeypatch)

    entrypoint.main(["--port", "8012"])

    assert calls == [{"app": "app.main:app", "host": "127.0.0.1", "port": 8012, "reload": False}]


def test_serve_always_binds_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """No authentication anywhere in this app: it must never bind 0.0.0.0.

    There is no flag for this — the only caller that wanted one was the
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


# ---------------------------------------------------------------- the settings


def test_settings_take_constructor_arguments(tmp_path: Path) -> None:
    """How every test in the suite configures an app."""
    configured = Settings(db_path=tmp_path / "app.db", port=9123, log_level="WARNING")

    assert configured.db_path == tmp_path / "app.db"
    assert configured.port == 9123
    assert configured.log_level == "WARNING"


def test_an_unprefixed_environment_variable_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """``PORT`` and ``DB_PATH`` are not this app's configuration any more.

    They are also names another tool may well have exported, which is why the
    internal hand-off is prefixed rather than bare.
    """
    monkeypatch.setenv("PORT", "9999")
    monkeypatch.setenv("DB_PATH", "/tmp/somewhere-else.db")

    assert Settings().port == 8000
    assert Settings().db_path == Path("./data/app.db")
