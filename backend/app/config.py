from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, overridable through the environment or a .env file.

    Field names map to upper-case environment variables (``db_path`` -> ``DB_PATH``,
    ``log_level`` -> ``LOG_LEVEL``, ...).
    """

    model_config = SettingsConfigDict(
        # Run from the repo root or from backend/ (make dev-api); pick up either.
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_path: Path = Path("./data/app.db")
    #: The port uvicorn binds. Read by ``python -m app``, which is what
    #: ``make dev-api`` runs — so this is a real knob, and the only one that works
    #: from ``.env`` (pydantic-settings never exports a ``.env`` value to
    #: ``os.environ``, so uvicorn's own ``$PORT`` would not see it).
    port: int = 8000
    # No API keys here. The Anthropic and Voyage keys live in the database, are
    # written through the Settings page and are read back only masked — one
    # source, not three. Do not add an env override back.
    #: Root log level for the application's own loggers; see ``app.logging_config``.
    log_level: str = "INFO"


settings = Settings()
