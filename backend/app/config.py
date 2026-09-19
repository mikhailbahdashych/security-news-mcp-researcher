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
    #: Optional ``ANTHROPIC_API_KEY``. This field is the *only* reason a key written
    #: into ``.env`` works at all: pydantic-settings reads ``.env`` into these fields
    #: and never exports it to ``os.environ``, so a service reading the environment
    #: directly would never see it. It is read through
    #: ``app.services.settings.get_effective_api_key`` and is never written to the
    #: database — see the precedence rules there.
    anthropic_api_key: str = ""
    #: Optional ``VOYAGE_API_KEY`` for the knowledge base's embeddings. Same story
    #: and same precedence as the Anthropic key above — see
    #: ``app.services.settings.get_effective_voyage_key``.
    voyage_api_key: str = ""
    #: Root log level for the application's own loggers; see ``app.logging_config``.
    log_level: str = "INFO"


settings = Settings()
