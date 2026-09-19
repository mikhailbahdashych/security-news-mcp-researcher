"""``python -m app`` — the one place uvicorn is told what to serve.

``make dev-api`` goes through here so that ``PORT`` is a real knob rather than a
decorative one. Reading it off :class:`Settings` is the *only* way a ``PORT``
written into ``.env`` can reach uvicorn: pydantic-settings loads ``.env`` into
its own fields and never exports it to ``os.environ``, so ``uvicorn --port
$PORT`` would silently serve 8000 anyway.

The bind address is not a knob. This app has no authentication of any kind and
it stores an Anthropic API key, so it listens on loopback and nothing else.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn

from app.config import Settings, settings

#: Loopback, unconditionally — see the module docstring.
HOST = "127.0.0.1"


def serve(*, reload: bool = False, app_settings: Settings | None = None) -> None:
    """Run the API on the configured port."""
    resolved = app_settings if app_settings is not None else settings
    # The import string, not the app object: uvicorn's reloader re-imports it in
    # the worker process and cannot reload an instance handed to it here.
    uvicorn.run("app.main:app", host=HOST, port=resolved.port, reload=reload)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m app",
        description="Serve the API (and the built SPA, if there is one) on $PORT.",
    )
    parser.add_argument("--reload", action="store_true", help="Restart on source changes.")
    args = parser.parse_args(argv)
    serve(reload=args.reload)


if __name__ == "__main__":
    main()
