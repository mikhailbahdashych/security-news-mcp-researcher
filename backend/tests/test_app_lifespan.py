"""What start-up and shutdown own: the turn registry and the orphaned-turn sweep."""

from sqlalchemy import select

from app.agent.turns import TurnRegistry, mark_interrupted
from app.db.models import ResearchSession


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
