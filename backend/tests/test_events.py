from datetime import datetime

from app.agent import events as ev


def test_turn_started_serialises_the_attach_context():
    event = ev.TurnStarted(
        turn_id="t-1",
        session_id=7,
        prompt="what happened?",
        attachments=[{"id": 3, "title": "An item", "url": "https://example.test/a"}],
        started_at=datetime(2026, 9, 16, 10, 0, 0),
    )
    name, payload = event.to_sse()
    assert name == "turn_started"
    assert payload == {
        "turn_id": "t-1",
        "session_id": 7,
        "prompt": "what happened?",
        "attachments": [{"id": 3, "title": "An item", "url": "https://example.test/a"}],
        "started_at": "2026-09-16T10:00:00",
    }
