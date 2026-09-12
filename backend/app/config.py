from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    port: int = 8000
    static_dir: Path = Path("./static")
    # Empty by default: in production the SPA is served same-origin, so no CORS is needed.
    cors_origins: list[str] = []


settings = Settings()
