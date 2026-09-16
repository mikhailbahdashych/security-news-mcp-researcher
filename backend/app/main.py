from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import api_router
from app.config import Settings
from app.config import settings as default_settings
from app.db.engine import create_db_engine, create_session_factory
from app.db.init import init_db
from app.logging_config import configure_logging
from app.mcp.manager import McpManager
from app.static import mount_spa


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
    try:
        yield
    finally:
        manager: McpManager | None = getattr(app.state, "mcp_manager", None)
        if manager is not None:
            await manager.aclose()
        app.state.session_factory = None
        app.state.db_engine = None
        await engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or default_settings
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
    # read the database path, CORS origins and static dir from here.
    app.state.settings = settings
    app.state.db_engine = None
    app.state.session_factory = None
    # Created empty and never connected here: the configured servers are read from
    # the database on first use, and the manager is what the lifespan closes.
    app.state.mcp_manager = McpManager()

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Deliberately no GZipMiddleware: it buffers responses, which turns the chat
    # SSE stream into a connection that appears to hang until the turn is over.
    # If compression is ever wanted, it has to exclude the streaming routes.

    # API routes first...
    app.include_router(api_router, prefix="/api")
    # ...and the SPA catch-all last, so it can never shadow an API route.
    mount_spa(app, settings.static_dir)
    return app


app = create_app()
