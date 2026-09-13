"""One-shot schema creation and default-settings seeding.

There is no migration tool in this project: the whole schema is declared in
``app.db.models`` and created here. Seeding only ever inserts keys that are
missing, so restarting the app never overwrites the user's own values.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.engine import get_engine, get_session_factory
from app.db.models import Base
from app.services.settings import seed_defaults


async def init_db(
    engine: AsyncEngine | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Create every table (if absent) and seed the default settings.

    Both arguments default to the process-wide engine/session factory; tests pass
    their own. Safe to call repeatedly.
    """
    engine = engine or get_engine()
    session_factory = session_factory or get_session_factory()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        await seed_defaults(session)
        await session.commit()


__all__ = ["init_db"]
