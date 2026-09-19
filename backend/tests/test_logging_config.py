"""The app's logging setup: records get out, and only once.

Before this existed nothing configured logging at all — uvicorn sets up its own
loggers and nothing else — so every ``logger.info`` under ``app.*`` was discarded
for having no handler, and a warning reached stderr through ``logging.lastResort``
with no timestamp and no logger name.
"""

from __future__ import annotations

import io
import logging

import pytest

from app.config import Settings
from app.logging_config import (
    HANDLER_NAME,
    configure_logging,
    installed_handler,
)


@pytest.fixture(autouse=True)
def restore_root_logging():
    """Put the root logger back exactly as it was.

    These tests add and remove the app's handler, and the rest of the suite (plus
    pytest's own capture) shares that logger.
    """
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        yield
    finally:
        root.handlers = handlers
        root.setLevel(level)


def _capture(handler: logging.Handler) -> io.StringIO:
    """Point the handler at a buffer instead of stderr."""
    stream = io.StringIO()
    handler.setStream(stream)
    return stream


def test_an_app_info_record_is_emitted_with_the_configured_format() -> None:
    handler = configure_logging("INFO")
    stream = _capture(handler)

    logging.getLogger("app.services.feeds").info("Refreshed %s feeds", 6)

    line = stream.getvalue()
    assert "Refreshed 6 feeds" in line
    assert "INFO" in line
    assert "app.services.feeds" in line
    # The timestamp: "2026-09-14 16:58:36 INFO ..." — a date, then the level.
    assert line.split()[0].count("-") == 2


def test_configure_logging_is_idempotent() -> None:
    first = configure_logging("INFO")
    second = configure_logging("INFO")

    assert first is second
    named = [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "name", None) == HANDLER_NAME
    ]
    assert len(named) == 1


def test_the_log_level_setting_is_honoured() -> None:
    handler = configure_logging("WARNING")
    stream = _capture(handler)

    logger = logging.getLogger("app.quiet")
    logger.info("not this one")
    logger.warning("but this one")

    assert "not this one" not in stream.getvalue()
    assert "but this one" in stream.getvalue()

    # And back up again: the level is re-applied, not only set on first install.
    configure_logging("DEBUG")
    logger.debug("now visible")
    assert "now visible" in stream.getvalue()


def test_an_unparsable_level_falls_back_to_info() -> None:
    handler = configure_logging("shout")
    stream = _capture(handler)

    logging.getLogger("app.fallback").info("still logged")

    assert "still logged" in stream.getvalue()


def test_create_app_installs_the_handler(app_factory, tmp_path) -> None:
    logging.getLogger().handlers = []

    app_factory(Settings(db_path=tmp_path / "app.db"))

    assert installed_handler() is not None


def test_create_app_honours_the_log_level_setting(app_factory, tmp_path) -> None:
    app_factory(
        Settings(
            db_path=tmp_path / "app.db",
            log_level="WARNING",
        )
    )

    handler = installed_handler()
    assert handler is not None
    assert handler.level == logging.WARNING


async def test_an_app_logger_reaches_caplog_at_info(caplog) -> None:
    configure_logging("INFO")

    with caplog.at_level(logging.INFO, logger="app.api.notes"):
        logging.getLogger("app.api.notes").info("Saved note %s", 3)

    assert "Saved note 3" in caplog.text
