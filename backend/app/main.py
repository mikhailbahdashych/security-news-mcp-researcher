from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import api_router
from app.config import Settings
from app.config import settings as default_settings
from app.db.engine import dispose_engine
from app.db.init import init_db
from app.static import mount_spa


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Application startup/shutdown hooks.

    Startup: create the schema and seed default settings (later tasks: start the
    feed scheduler). Shutdown: dispose the database engine (later tasks: close MCP
    sessions).

    Note that Starlette only runs this for a real server; the test suite drives the
    app through ``ASGITransport``, which skips the lifespan, so tests initialise
    their own database.
    """
    await init_db()
    yield
    await dispose_engine()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or default_settings
    app = FastAPI(
        title="Security News MCP Researcher",
        version=__version__,
        lifespan=lifespan,
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # API routes first...
    app.include_router(api_router, prefix="/api")
    # ...and the SPA catch-all last, so it can never shadow an API route.
    mount_spa(app, settings.static_dir)
    return app


app = create_app()
