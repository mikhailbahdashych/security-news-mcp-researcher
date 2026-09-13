"""One-shot schema creation and default-settings seeding.

There is no migration tool in this project: the whole schema is declared in
``app.db.models`` and created here. Seeding only ever inserts keys that are
missing, so restarting the app never overwrites the user's own values.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.engine import create_session_factory
from app.db.models import Base
from app.services.settings import seed_defaults


async def init_db(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Create every table (if absent) and seed the default settings.

    The engine is explicit so the caller decides which database is initialised;
    ``session_factory`` defaults to a fresh factory over that same engine. Safe to
    call repeatedly.
    """
    session_factory = session_factory or create_session_factory(engine)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        await seed_defaults(session)
        await session.commit()


__all__ = ["init_db"]
