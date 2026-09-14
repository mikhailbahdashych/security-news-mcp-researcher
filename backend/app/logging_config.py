"""The application's logging setup — one handler, installed once.

Without this the app's own log records go nowhere. ``uvicorn`` configures its own
loggers (``uvicorn``, ``uvicorn.error``, ``uvicorn.access``) and nothing else, so
every ``logger.info`` under ``app.*`` was dropped for having no handler, and a
``logger.warning`` reached stderr through ``logging.lastResort`` — unformatted,
with no timestamp and no logger name. A feed that failed to refresh or an MCP
server that would not start was therefore invisible or unattributable.

Two properties matter:

* **Idempotent.** ``create_app`` is called once per server but many times per test
  run, and adding a handler per call would print every record N times. The handler
  is tagged with :data:`HANDLER_NAME` and reused.
* **It leaves uvicorn alone.** This adds a handler to the *root* logger; uvicorn's
  own loggers carry their handlers with ``propagate`` off for access logs, so the
  request log keeps its format and nothing is duplicated.
"""

from __future__ import annotations

import logging

#: ``asctime`` first so lines sort, then the level, then who emitted it. No
#: process/thread ids: this is a single-process local app.
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

DEFAULT_LOG_LEVEL = "INFO"

#: How :func:`configure_logging` recognises the handler it already installed.
HANDLER_NAME = "security-news-app"


def _resolve_level(level: str | int | None) -> int:
    """``"debug"`` / ``"INFO"`` / ``20`` -> a logging level, defaulting to INFO."""
    if isinstance(level, int):
        return level
    names = logging.getLevelNamesMapping()
    return names.get((level or DEFAULT_LOG_LEVEL).strip().upper(), logging.INFO)


def installed_handler() -> logging.Handler | None:
    """The handler :func:`configure_logging` installed, if it is still there."""
    return next(
        (
            handler
            for handler in logging.getLogger().handlers
            if getattr(handler, "name", None) == HANDLER_NAME
        ),
        None,
    )


def configure_logging(level: str | int | None = DEFAULT_LOG_LEVEL) -> logging.Handler:
    """Install (or re-level) the application's stderr handler. Safe to call twice.

    Returns the handler, so a caller — a test, mostly — can look at where records
    actually go without reaching into the root logger's list.
    """
    resolved = _resolve_level(level)
    root = logging.getLogger()

    handler = installed_handler()
    if handler is None:
        handler = logging.StreamHandler()
        handler.name = HANDLER_NAME
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        root.addHandler(handler)

    handler.setLevel(resolved)
    root.setLevel(resolved)
    return handler


__all__ = [
    "DATE_FORMAT",
    "DEFAULT_LOG_LEVEL",
    "HANDLER_NAME",
    "LOG_FORMAT",
    "configure_logging",
    "installed_handler",
]
