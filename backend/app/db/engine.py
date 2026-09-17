"""Async SQLAlchemy engine and session factory for the local SQLite file.

There is no module-level engine on purpose: the application builds one per app from
the ``Settings`` it was handed (see ``create_app``/``lifespan`` in ``app.main``) and
keeps it on ``app.state``, so an app constructed with a different ``db_path`` really
does use that database. Tests build their own the same way.

Every connection this module opens loads **sqlite-vec**. That is not an optimisation
for the knowledge base's sake: ``kb_chunk_vec`` is a ``vec0`` virtual table, so a
connection without the extension cannot read the schema at all — the module is
unknown and ``VACUUM`` / ``.dump`` fail on it. Any future process that opens this
file (a CLI, a backup job) must go through here, or load the extension the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.util import await_only

try:  # pragma: no cover - the wheel is a declared dependency; this is the diagnosis
    import sqlite_vec
except ModuleNotFoundError:  # pragma: no cover - see _load_sqlite_vec
    sqlite_vec = None

# WAL lets the feed poller write while a request reads; NORMAL synchronous is the
# usual companion for WAL; busy_timeout turns "database is locked" into a short
# wait; foreign_keys is off by default in SQLite and our cascades depend on it.
_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
)


#: What to tell a user whose installation cannot load the extension. This is fatal
#: by design — ``kb_chunk_vec`` is a ``vec0`` table, so without the extension the
#: schema cannot be read at all — so the message has to name the fix rather than
#: leave a traceback to be interpreted.
MISSING_WHEEL = (
    "sqlite-vec is not installed, and this database cannot be opened without it "
    "(kb_chunk_vec is a vec0 virtual table). Run `uv sync` in backend/."
)
NO_LOADABLE_EXTENSIONS = (
    "This Python cannot load loadable SQLite extensions: its sqlite3 module has no "
    "enable_load_extension, so it was built with --disable-loadable-sqlite-extensions. "
    "The knowledge base needs sqlite-vec, so the application needs a Python build "
    "that allows it."
)


@dataclass(frozen=True, slots=True)
class ExtensionStatus:
    """What the SQLite build and the loaded extension can do.

    Surfaced by the Settings "index stats" panel, which is the only place a user
    can find out *why* vector search is unavailable on their machine.
    """

    vec_version: str
    fts5: bool
    sqlite_version: str


def _load_sqlite_vec(dbapi_connection: Any) -> None:
    """Load sqlite-vec into a freshly opened aiosqlite connection.

    ``enable_load_extension`` and ``load_extension`` are C-API calls, not SQL, and
    aiosqlite runs the real ``sqlite3.Connection`` on its own worker thread — so
    they have to be queued onto that thread rather than called from here. The
    ``connect`` event fires inside SQLAlchemy's greenlet, which is what makes
    ``await_only`` legal (and what makes calling the private ``_conn`` from this
    thread illegal).

    Extension loading is re-disabled afterwards: leaving it on would let any later
    ``SELECT load_extension(...)`` pull arbitrary code into the process.

    The two ways this can fail — no wheel, and a CPython built without loadable
    extension support — are both fatal and both raise :class:`RuntimeError` naming
    the fix. A bare ``ModuleNotFoundError`` at import or an ``AttributeError`` on
    the first connect tells the user nothing they can act on, and takes
    :func:`extension_status` down with it.
    """
    if sqlite_vec is None:
        raise RuntimeError(MISSING_WHEEL)
    driver_connection = dbapi_connection.driver_connection
    if not hasattr(driver_connection, "enable_load_extension"):
        raise RuntimeError(NO_LOADABLE_EXTENSIONS)
    await_only(driver_connection.enable_load_extension(True))
    try:
        await_only(driver_connection.load_extension(sqlite_vec.loadable_path()))
    finally:
        await_only(driver_connection.enable_load_extension(False))


def create_db_engine(db_path: Path | str) -> AsyncEngine:
    """Build an engine for ``db_path``, creating its parent directory if needed."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _prepare_connection(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            for pragma in _PRAGMAS:
                cursor.execute(pragma)
        finally:
            cursor.close()
        _load_sqlite_vec(dbapi_connection)

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory whose instances stay usable after ``commit()``."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def extension_status(executor: AsyncConnection | AsyncSession) -> ExtensionStatus:
    """Report the loaded ``sqlite-vec`` version and whether FTS5 is compiled in.

    Both halves of the knowledge base's index depend on something outside the
    application's control — a wheel that has to carry a loadable library, and a
    SQLite build that has to have been compiled with FTS5 — so the Settings panel
    states them rather than leaving a failed search to explain itself.

    An absent ``vec_version()`` is reported as ``""`` rather than raised. This
    function is the one place that explains *why* vector search is unavailable, so
    it must survive the connection that cannot answer it.
    """
    try:
        vec_version = (await executor.execute(text("SELECT vec_version()"))).scalar_one()
    except OperationalError:
        vec_version = ""
    sqlite_version = (await executor.execute(text("SELECT sqlite_version()"))).scalar_one()
    options = {row[0] for row in (await executor.execute(text("PRAGMA compile_options"))).all()}
    return ExtensionStatus(
        vec_version=str(vec_version),
        fts5="ENABLE_FTS5" in options,
        sqlite_version=str(sqlite_version),
    )


__all__ = [
    "MISSING_WHEEL",
    "NO_LOADABLE_EXTENSIONS",
    "ExtensionStatus",
    "create_db_engine",
    "create_session_factory",
    "extension_status",
]
