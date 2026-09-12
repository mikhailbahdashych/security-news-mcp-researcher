from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import api_router
from app.config import Settings
from app.config import settings as default_settings
from app.static import mount_spa


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Application startup/shutdown hooks.

    Startup (later tasks): initialise the SQLite schema, start the feed scheduler.
    Shutdown (later tasks): close MCP sessions and the database connection.
    """
    yield


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
