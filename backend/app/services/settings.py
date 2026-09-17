"""The application's key/value settings store.

Everything configurable lives in the ``settings`` table as TEXT, with the typed
accessors below doing the parsing. Callers read through :func:`get_effective_api_key`
rather than the raw key so that an ``ANTHROPIC_API_KEY`` supplied from outside the
database — the process environment, or ``.env`` by way of :class:`app.config.Settings`
— can override the stored value without ever being written to the database.

The raw API key must never reach a response body or a log line — use
:func:`mask_key` for anything user-visible.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Setting, utcnow
from app.kb.schema import KB_SCHEMA_VERSION_KEY, default_schema_version

logger = logging.getLogger(__name__)

API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"

#: Where the key actually used for Anthropic calls came from. ``"env"`` covers both
#: spellings of "configured outside the app" — the process environment and the
#: ``ANTHROPIC_API_KEY`` line of ``.env``, which reaches us as ``Settings``.
KeySource = Literal["env", "stored", "none"]

DEFAULT_NOTE_TEMPLATE = """For each news item, produce a section with these headings:
## {Item title}
**What happened** — …
**Root cause** — …
**Why it matters** — …
**Lessons learned** — …
**Recommended actions for teams** — …"""

DEFAULT_SETTINGS: dict[str, str] = {
    "anthropic_api_key": "",
    "model": "claude-opus-5",
    "effort": "high",
    "thinking_display": "summarized",
    "web_search_enabled": "true",
    "web_search_max_uses": "8",
    "web_fetch_enabled": "true",
    "max_tool_turns": "12",
    "note_template": DEFAULT_NOTE_TEMPLATE,
    "system_prompt_extra": "",
    "feed_timeout_s": "15",
    # What the knowledge base's two virtual tables were actually built with. Not a
    # preference: the app compares it with the constants in ``app.kb.schema`` and
    # reports "index format outdated" when they disagree.
    KB_SCHEMA_VERSION_KEY: default_schema_version(),
}

#: Settings whose value is one of a closed set, and what that set is.
#:
#: The single source for both readers: ``GET /api/settings`` renders these and
#: ``app.agent.providers.turn_settings`` sends them to the API. They used to
#: coerce separately — the page repaired a hand-edited ``effort`` for display
#: while the turn still sent the raw value, so the user saw "high" and every
#: message came back 400 with nothing connecting the two.
#:
#: ``tests/test_settings_service.py`` pins these against the ``Literal``s in
#: ``app.schemas.settings``, which is where the API's own promise lives; the
#: service deliberately does not import them, so the layering stays one-way.
ALLOWED_VALUES: dict[str, tuple[str, ...]] = {
    "effort": ("low", "medium", "high", "xhigh", "max"),
    "thinking_display": ("summarized", "omitted"),
}

_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "off", ""})


def mask_key(key: str | None) -> str:
    """Render an API key for display: ``sk-ant-…a1b2``. Empty key -> empty string.

    Only the last four characters are ever revealed, and only when the key is long
    enough that those four characters are not most of it.
    """
    key = (key or "").strip()
    if not key:
        return ""
    prefix = "sk-ant-" if key.startswith("sk-ant-") else ""
    suffix = key[-4:] if len(key) - len(prefix) > 4 else ""
    return f"{prefix}…{suffix}"


async def get(session: AsyncSession, key: str, default: str | None = None) -> str | None:
    """Read one setting, falling back to ``default`` when the row is absent."""
    value = await session.scalar(select(Setting.value).where(Setting.key == key))
    return default if value is None else value


async def get_all(session: AsyncSession) -> dict[str, str]:
    """Every stored setting as a plain dict."""
    rows = (await session.execute(select(Setting.key, Setting.value))).all()
    return {key: value or "" for key, value in rows}


async def set_value(session: AsyncSession, key: str, value: str) -> None:
    """Insert or update a single setting."""
    await set_many(session, {key: value})


async def set_many(session: AsyncSession, values: Mapping[str, str]) -> None:
    """Insert or update several settings in one statement."""
    if not values:
        return

    now = utcnow()
    statement = sqlite_insert(Setting).values(
        [
            {"key": key, "value": value, "created_at": now, "updated_at": now}
            for key, value in values.items()
        ]
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[Setting.key],
            set_={"value": statement.excluded.value, "updated_at": now},
        )
    )
    # Rows written by Core statements are invisible to objects already in the
    # identity map; expiring keeps a long-lived session honest.
    session.expire_all()


async def seed_defaults(session: AsyncSession) -> None:
    """Insert any default that is not already stored, leaving existing values alone."""
    existing = set(await get_all(session))
    missing = {key: value for key, value in DEFAULT_SETTINGS.items() if key not in existing}
    await set_many(session, missing)


async def get_str(session: AsyncSession, key: str) -> str:
    """A setting as a string, falling back to its documented default."""
    value = await get(session, key)
    if value is None:
        return DEFAULT_SETTINGS.get(key, "")
    return value


async def get_choice(session: AsyncSession, key: str) -> str:
    """A setting from :data:`ALLOWED_VALUES`, falling back to its default.

    ``PUT /api/settings`` validates these fields, so the only way an unknown
    value gets in is a hand-edited database. Answering that with a 500 from
    response validation would lock the user out of the Settings page that could
    fix it, and sending it to the Anthropic API would 400 every message — so it
    is coerced here, once, for every caller, and the row is left for the next
    PUT to overwrite. The warning is the only trace that it is being ignored.
    """
    allowed = ALLOWED_VALUES[key]
    value = await get_str(session, key)
    if value in allowed:
        return value
    logger.warning(
        "Stored %s is not one of %s; falling back to the default", key, ", ".join(allowed)
    )
    return DEFAULT_SETTINGS[key]


async def get_bool(session: AsyncSession, key: str) -> bool:
    """A setting as a boolean; unparsable values fall back to the default."""
    return _parse_bool(await get(session, key), key)


async def get_int(session: AsyncSession, key: str) -> int:
    """A setting as an integer; unparsable values fall back to the default."""
    return _parse_int(await get(session, key), key)


def external_api_key(settings: Settings | None = None) -> str:
    """The key configured outside the database, or ``""``.

    Two places count, in this order: the real process environment
    (``ANTHROPIC_API_KEY=... make dev-api``), then the app's :class:`Settings`,
    which is what actually loads a key written into ``.env`` — pydantic-settings
    reads ``.env`` into its own fields and never exports it to ``os.environ``, so
    reading the environment alone silently ignored it.

    ``settings`` is optional so that this stays a plain service function: passing
    ``None`` means "no ``Settings`` source", which is what a caller that has no app
    handy (and every test that has not opted in) wants.
    """
    from_env = (os.environ.get(API_KEY_ENV_VAR) or "").strip()
    if from_env:
        return from_env
    return (settings.anthropic_api_key or "").strip() if settings is not None else ""


async def get_effective_api_key(session: AsyncSession, settings: Settings | None = None) -> str:
    """The API key actually used for Anthropic calls.

    Precedence: process environment, then the app ``Settings`` (i.e. ``.env``),
    then the key stored in the database. Neither external value is ever written
    back to the database.
    """
    external = external_api_key(settings)
    if external:
        return external
    return (await get(session, "anthropic_api_key") or "").strip()


async def get_key_source(session: AsyncSession, settings: Settings | None = None) -> KeySource:
    """Which of the three sources :func:`get_effective_api_key` would use.

    Purely informational: ``has_api_key`` still means "a key is stored in this
    database", so the Settings page can keep saying whether *its* key is set while
    still telling the user that an external one is overriding it.
    """
    if external_api_key(settings):
        return "env"
    if (await get(session, "anthropic_api_key") or "").strip():
        return "stored"
    return "none"


def _parse_bool(value: str | None, key: str) -> bool:
    if value is not None:
        lowered = value.strip().lower()
        if lowered in _TRUE_VALUES:
            return True
        if lowered in _FALSE_VALUES:
            return False
    fallback = DEFAULT_SETTINGS.get(key, "false")
    return fallback.strip().lower() in _TRUE_VALUES


def _parse_int(value: str | None, key: str) -> int:
    for candidate in (value, DEFAULT_SETTINGS.get(key)):
        if candidate is None:
            continue
        try:
            return int(candidate.strip())
        except ValueError:
            continue
    return 0


__all__ = [
    "ALLOWED_VALUES",
    "API_KEY_ENV_VAR",
    "DEFAULT_NOTE_TEMPLATE",
    "DEFAULT_SETTINGS",
    "KeySource",
    "external_api_key",
    "get",
    "get_all",
    "get_bool",
    "get_choice",
    "get_effective_api_key",
    "get_int",
    "get_key_source",
    "get_str",
    "mask_key",
    "seed_defaults",
    "set_many",
    "set_value",
]
