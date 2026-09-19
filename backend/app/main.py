import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.agent.turns import TurnRegistry, mark_interrupted
from app.api import api_router
from app.config import Settings
from app.db.engine import create_db_engine, create_session_factory
from app.db.init import init_db
from app.logging_config import configure_logging
from app.mcp.manager import McpManager
from app.services.http import impersonation_available

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application startup/shutdown hooks.

    Startup: open the database named by *this app's* settings, create the schema and
    seed the default settings, then publish the session factory on ``app.state`` for
    ``get_db`` (later tasks: start the feed scheduler). Shutdown: close every MCP
    connection — which is what terminates their stdio subprocesses — and then
    dispose the engine.

    MCP servers are deliberately **not** connected here. A server that is slow to
    start, or that never speaks protocol at all, would otherwise hold up boot and
    the health check; connections are made on first use instead.

    Note that Starlette only runs this for a real server; the test suite drives the
    app through ``ASGITransport``, which skips the lifespan, so tests initialise
    their own database and override ``get_db``.
    """
    settings: Settings = app.state.settings
    engine = create_db_engine(settings.db_path)
    app.state.db_engine = engine
    app.state.session_factory = create_session_factory(engine)

    await init_db(engine, app.state.session_factory)

    # A turn is a task in *this* process, so one that was running when the last
    # process stopped can never be resumed — the row would otherwise claim a turn
    # is in flight forever, and the Chat page would wait for events nobody sends.
    interrupted = await mark_interrupted(app.state.session_factory)
    if interrupted:
        logger.warning(
            "%d research turn(s) were running when the process last stopped; marked interrupted",
            interrupted,
        )

    # An optional import, so nothing else in the app can say it is missing — and
    # what it costs is invisible until a feed is refreshed: the 403 retry simply
    # never happens. The live case was a --reload dev server that picked up the
    # retry code before its venv had the wheel.
    if not impersonation_available():
        logger.warning(
            "curl_cffi is not installed; feeds behind TLS-fingerprint bot protection "
            "(e.g. CISA) will stay 403 — run `uv sync`"
        )

    try:
        yield
    finally:
        # Before the engine goes: a turn still running writes rows as it ends.
        registry: TurnRegistry | None = getattr(app.state, "turn_registry", None)
        if registry is not None:
            await registry.drain()
        manager: McpManager | None = getattr(app.state, "mcp_manager", None)
        if manager is not None:
            await manager.aclose()
        app.state.session_factory = None
        app.state.db_engine = None
        await engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    # Before anything else, so that whatever the rest of start-up logs is actually
    # seen and formatted. Idempotent, so the test suite's many apps share one
    # handler instead of multiplying every record. See app/logging_config.py.
    configure_logging(settings.log_level)
    app = FastAPI(
        title="Security News MCP Researcher",
        version=__version__,
        lifespan=lifespan,
    )
    # The single source of truth for this app: the lifespan and every dependency
    # read the database path from here.
    app.state.settings = settings
    app.state.db_engine = None
    app.state.session_factory = None
    # Created empty and never connected here: the configured servers are read from
    # the database on first use, and the manager is what the lifespan closes.
    app.state.mcp_manager = McpManager()
    # Here rather than in the lifespan for the same reason: it holds nothing until
    # a turn starts, and the test suite (which skips the lifespan) needs one too.
    app.state.turn_registry = TurnRegistry()

    # Deliberately no CORS middleware: the SPA reaches this API through Vite's
    # `/api` proxy, so every request is same-origin. An API with no auth at all,
    # holding the user's Anthropic key, has no business inviting other origins.

    # Deliberately no GZipMiddleware: it buffers responses, which turns the chat
    # SSE stream into a connection that appears to hang until the turn is over.
    # If compression is ever wanted, it has to exclude the streaming routes.

    # The whole app: there is no static-file route. The SPA is served by Vite
    # (`make dev-web`), which proxies /api here, so an unknown path is FastAPI's
    # own JSON 404 and nothing can shadow an API route.
    app.include_router(api_router, prefix="/api")
    return app


app = create_app()
