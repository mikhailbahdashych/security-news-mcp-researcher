import json
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, overridable through the environment or a .env file.

    Field names map to upper-case environment variables (``db_path`` -> ``DB_PATH``,
    ``static_dir`` -> ``STATIC_DIR``, ...).
    """

    model_config = SettingsConfigDict(
        # Run from the repo root (Docker) or from backend/ (make dev-api); pick up either.
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_path: Path = Path("./data/app.db")
    #: The port uvicorn binds. Read by ``python -m app``, which is what both
    #: ``make dev-api`` and the image's ``CMD`` run — so this is a real knob, and
    #: the only one that works from ``.env`` (pydantic-settings never exports a
    #: ``.env`` value to ``os.environ``, so uvicorn's own ``$PORT`` would not see it).
    port: int = 8000
    static_dir: Path = Path("./static")
    # Empty by default: in production the SPA is served same-origin, so no CORS is needed.
    #: ``NoDecode`` because pydantic-settings JSON-decodes a list field's env value
    #: *before* validation: ``CORS_ORIGINS=http://localhost:5173`` used to raise a
    #: ``SettingsError`` at import time, which is a stack trace with no app behind
    #: it. The validator below takes the comma-separated form a human writes.
    cors_origins: Annotated[list[str], NoDecode] = []
    #: Optional ``ANTHROPIC_API_KEY``. This field is the *only* reason a key written
    #: into ``.env`` works at all: pydantic-settings reads ``.env`` into these fields
    #: and never exports it to ``os.environ``, so a service reading the environment
    #: directly would never see it. It is read through
    #: ``app.services.settings.get_effective_api_key`` and is never written to the
    #: database — see the precedence rules there.
    anthropic_api_key: str = ""
    #: Root log level for the application's own loggers; see ``app.logging_config``.
    log_level: str = "INFO"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, value: Any) -> Any:
        """Accept ``a,b``, ``["a","b"]`` and an already-parsed list, and refuse
        anything that is not an explicit origin.

        The JSON form still works because a ``.env`` written against the old
        behaviour must keep working; everything else is split on commas, with
        blank entries dropped so a trailing comma is not an origin. A value that
        merely *looks* like JSON falls through to the comma form rather than
        escaping as a ``JSONDecodeError``, which is the same "stack trace with no
        app behind it" this validator exists to remove.

        The check at the end is a security boundary, not tidiness — see
        :func:`_check_origins`.
        """
        if isinstance(value, str):
            origins = _split_origins(value)
        elif isinstance(value, (list, tuple)):
            # A list from the JSON form, a test, or `Settings(cors_origins=[...])`:
            # the wildcard must not have a spelling that skips the check.
            origins = [str(item).strip() for item in value if str(item).strip()]
        else:
            return value
        _check_origins(origins)
        return origins


def _split_origins(value: str) -> list[str]:
    text = value.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass  # Not JSON after all; fall through to the comma form.
    return [part.strip() for part in text.split(",") if part.strip()]


def _is_origin(candidate: str) -> bool:
    """An ``Origin`` header is ``scheme://host[:port]`` and nothing else."""
    parsed = urlsplit(candidate)
    return (
        parsed.scheme in ("http", "https")
        and bool(parsed.netloc)
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
    )


def _check_origins(origins: list[str]) -> None:
    """Refuse the wildcard, and anything that could never match an origin.

    This app has no authentication of any kind, so the origin list is the only
    thing between a random page in the user's browser and an API that can read
    and write the stored Anthropic key. ``*`` is therefore not a shortcut for
    "open it from my other laptop" — it is "every site I visit may spend my key".

    Everything else is refused because ``CORSMiddleware`` compares the ``Origin``
    header by exact string: ``localhost:5173`` or ``http://a.test/app`` is a rule
    that can never fire, and a rule that silently never fires is worse than one
    that says so at start-up.
    """
    if "*" in origins:
        raise ValueError(
            "CORS_ORIGINS must list explicit origins; '*' would let any page in the "
            "browser read and write this API, which has no authentication."
        )
    invalid = [origin for origin in origins if not _is_origin(origin)]
    if invalid:
        raise ValueError(
            "CORS_ORIGINS entries must be http(s) origins with no path, "
            f"e.g. http://localhost:5173 — got {', '.join(repr(o) for o in invalid)}."
        )


settings = Settings()
