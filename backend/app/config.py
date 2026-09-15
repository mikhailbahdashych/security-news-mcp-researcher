import json
from pathlib import Path
from typing import Annotated, Any

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
        """Accept ``a,b``, ``["a","b"]`` and an already-parsed list.

        The JSON form still works because a ``.env`` written against the old
        behaviour must keep working; everything else is split on commas, with
        blank entries dropped so a trailing comma is not an origin.
        """
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            return json.loads(text)
        return [part.strip() for part in text.split(",") if part.strip()]


settings = Settings()
