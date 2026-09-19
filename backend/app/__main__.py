"""``python -m app`` — the one place uvicorn is told what to serve.

Everything configurable at start-up is a flag here: ``--port``, ``--db-path``,
``--log-level``, ``--reload``. There is no ``.env`` file and no environment
variable to remember; ``make dev-api`` passes the flags from ``PORT`` / ``DB`` /
``LOG``.

The bind address is not a knob. This app has no authentication of any kind and
it stores an Anthropic API key, so it listens on loopback and nothing else.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from app.config import Settings

#: Loopback, unconditionally — see the module docstring.
HOST = "127.0.0.1"


def serve(*, port: int, db_path: Path, log_level: str, reload: bool = False) -> None:
    """Run the API on *port*, against *db_path*."""
    # An internal hand-off, not a user-facing feature: uvicorn's reloader re-imports
    # app.main — and so app.config — in a *worker process*, where a Settings object
    # built here could never arrive. The environment is the only channel that
    # crosses that boundary. SNR_-prefixed so an ambient PORT or DB_PATH meant for
    # some other tool cannot be mistaken for this app's configuration, and so that
    # nobody reads these as a documented way to run it. The port is not among them:
    # uvicorn binds it from the argument below and nothing in the app reads it.
    os.environ["SNR_DB_PATH"] = str(db_path)
    os.environ["SNR_LOG_LEVEL"] = log_level
    # The import string, not the app object: uvicorn's reloader re-imports it in
    # the worker process and cannot reload an instance handed to it here.
    uvicorn.run("app.main:app", host=HOST, port=port, reload=reload)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m app",
        description=f"Serve the API on {HOST}.",
    )
    # The defaults are Settings' own, so there is one place they are written down.
    settings = Settings()
    parser.add_argument(
        "--port", type=int, default=settings.port, help=f"Port to bind (default {settings.port})."
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=settings.db_path,
        help=f"SQLite database file (default {settings.db_path}).",
    )
    parser.add_argument(
        "--log-level",
        default=settings.log_level,
        help=f"DEBUG, INFO, WARNING or ERROR (default {settings.log_level}).",
    )
    parser.add_argument("--reload", action="store_true", help="Restart on source changes.")
    args = parser.parse_args(argv)
    serve(port=args.port, db_path=args.db_path, log_level=args.log_level, reload=args.reload)


if __name__ == "__main__":
    main()
