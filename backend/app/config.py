from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings.

    Two ways in, and no third. **Callers** — the whole test suite — pass values to
    the constructor: ``Settings(db_path=tmp_path / "app.db")``. **The entrypoint**
    (``app/__main__.py``) puts its parsed command-line flags into the process
    environment as ``SNR_*`` before it starts uvicorn, because uvicorn's reloader
    re-imports this module in a worker process where an object built in ``main()``
    would never arrive. Those variables are that hand-off and nothing else: they
    are not configuration, not documented and not a supported way to run the app.

    There is no ``.env`` file and no unprefixed environment variable. Run the app
    with flags (``python -m app --port 8012 --db-path ./data/other.db``), or
    through the Makefile, which passes them for you.
    """

    model_config = SettingsConfigDict(env_prefix="SNR_", extra="ignore")

    db_path: Path = Path("./data/app.db")
    #: The port uvicorn binds; see ``app/__main__.py``.
    port: int = 8000
    # No API keys here. The Anthropic and Voyage keys live in the database, are
    # written through the Settings page and are read back only masked — one
    # source, not three. Do not add an env override back.
    #: Root log level for the application's own loggers; see ``app.logging_config``.
    log_level: str = "INFO"


# Deliberately no module-level ``settings = Settings()``. It would be built the
# moment anything imports this module — for ``python -m app`` that is before the
# flags are parsed — and without ``--reload`` uvicorn imports ``app.main`` in that
# same process, so ``--db-path`` would silently open the default database.
# ``create_app()`` builds its own when it is called.
