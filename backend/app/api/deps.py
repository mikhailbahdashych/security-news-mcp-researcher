"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """One database session per request.

    Nothing is committed automatically: routes commit their own writes, so a route
    that raises leaves the database untouched.
    """
    async with get_session_factory()() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db)]


__all__ = ["DbSession", "get_db"]
