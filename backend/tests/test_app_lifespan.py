"""What start-up and shutdown own: the turn registry and the orphaned-turn sweep.

Two of these tests run the **real** lifespan (``ASGITransport`` skips it), because
the sweep and the drain are wiring that nothing else exercises: with both calls
deleted the rest of the suite stayed green, and the failure they prevent only
shows up on a restart or a shutdown.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
from httpx2 import ASGITransport
from sqlalchemy import select

from app.agent import events as ev
from app.agent.turns import TurnRegistry, mark_interrupted
from app.config import Settings
from app.db.engine import create_db_engine, create_session_factory
from app.db.init import init_db
from app.db.models import ResearchSession
from app.main import create_app


async def _seed(db_path: Path, **columns) -> int:
    """An initialised database with one research session in it."""
    engine = create_db_engine(db_path)
    try:
        await init_db(engine, create_session_factory(engine))
        async with create_session_factory(engine)() as session:
            row = ResearchSession(title="t", **columns)
            session.add(row)
            await session.commit()
            return row.id
    finally:
        await engine.dispose()


async def _read_status(db_path: Path, session_id: int) -> str:
    engine = create_db_engine(db_path)
    try:
        async with create_session_factory(engine)() as session:
            row = (
                await session.execute(
                    select(ResearchSession).where(ResearchSession.id == session_id)
                )
            ).scalar_one()
            return row.turn_status
    finally:
        await engine.dispose()


async def never_ends() -> AsyncIterator[ev.AgentEvent]:
    """A turn that only a cancel can stop."""
    while True:
        await asyncio.sleep(0.01)
        yield ev.TextDelta(text="x")


async def test_mark_interrupted_flips_only_running_rows(session_factory):
    async with session_factory() as session:
        session.add_all(
            [
                ResearchSession(title="a", turn_status="running"),
                ResearchSession(title="b", turn_status="idle"),
                ResearchSession(title="c", turn_status="interrupted"),
            ]
        )
        await session.commit()

    assert await mark_interrupted(session_factory) == 1

    async with session_factory() as session:
        rows = (
            (await session.execute(select(ResearchSession).order_by(ResearchSession.title)))
            .scalars()
            .all()
        )
        assert [row.turn_status for row in rows] == ["interrupted", "idle", "interrupted"]


def test_create_app_installs_a_turn_registry(app):
    assert isinstance(app.state.turn_registry, TurnRegistry)


async def test_the_lifespan_marks_orphaned_rows_interrupted(tmp_path: Path):
    """A turn is a task in *this* process: one left ``running`` cannot be resumed.

    Without the startup sweep the row claims a turn is in flight forever and the
    Chat page waits for events nobody will ever send.
    """
    db_path = tmp_path / "app.db"
    session_id = await _seed(db_path, turn_status="running")

    application = create_app(Settings(db_path=db_path, static_dir=tmp_path / "absent"))
    async with application.router.lifespan_context(application):
        async with httpx2.AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            listed = (await client.get("/api/sessions")).json()["sessions"]

    assert [row["turn_status"] for row in listed if row["id"] == session_id] == ["interrupted"]


async def test_the_lifespan_drains_a_running_turn(tmp_path: Path):
    """Shutdown stops what is still running, before the engine goes away.

    A turn left running past the ``finally`` writes its last rows into a disposed
    engine — and its subscribers wait for a ``done`` that never comes.
    """
    db_path = tmp_path / "app.db"
    session_id = await _seed(db_path)

    application = create_app(Settings(db_path=db_path, static_dir=tmp_path / "absent"))
    async with application.router.lifespan_context(application):
        turn = await application.state.turn_registry.start(
            session_id=session_id,
            session_factory=application.state.session_factory,
            generator=never_ends(),
            client=None,
            prompt="q",
            attachments=[],
        )
        assert application.state.turn_registry.is_running(session_id)

    assert turn.log.closed is True
    assert turn.log.events[-1].type == "done"
    assert application.state.turn_registry.running_ids() == []
    assert await _read_status(db_path, session_id) == "idle"
