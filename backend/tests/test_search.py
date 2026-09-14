"""Cross-entity search: one query box over feed items, sessions and notes.

The headline behaviour is that a CVE id typed once finds everything the user has
accumulated about it, whichever shape it took — a headline in the inbox, a
question asked in a research chat, a line in a generated note.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest

from app.db.models import Feed, FeedItem, Message, Note, ResearchSession, utcnow
from app.services import search as search_service

CVE = "CVE-2026-12345"


async def add_feed(db_session) -> Feed:
    feed = Feed(url="https://example.test/rss", title="Example")
    db_session.add(feed)
    await db_session.flush()
    return feed


async def add_item(db_session, feed_id: int, guid: str, **kwargs) -> FeedItem:
    item = FeedItem(
        feed_id=feed_id,
        guid=guid,
        url=f"https://example.test/{guid}",
        title=kwargs.pop("title", "An advisory"),
        published_at=kwargs.pop("published_at", utcnow()),
        **kwargs,
    )
    db_session.add(item)
    await db_session.flush()
    return item


async def add_session(db_session, *, title=None, archived=False, previews=()) -> ResearchSession:
    research = ResearchSession(title=title, archived=archived)
    db_session.add(research)
    await db_session.flush()
    for seq, preview in enumerate(previews, start=1):
        db_session.add(
            Message(
                session_id=research.id,
                seq=seq,
                role="user",
                kind="user",
                content_json=[{"type": "text", "text": preview}],
                text_preview=preview,
            )
        )
    await db_session.flush()
    return research


async def add_note(db_session, *, title=None, body_md="body") -> Note:
    note = Note(title=title, body_md=body_md)
    db_session.add(note)
    await db_session.flush()
    return note


@pytest.fixture
async def haystack(db_session):
    """One match of each kind, plus a decoy of each kind that must not match."""
    feed = await add_feed(db_session)
    item = await add_item(
        db_session,
        feed.id,
        "g1",
        title="AcmeVPN advisory",
        summary="Pre-auth remote code execution.",
        content_text=f"Vendors confirmed {CVE} is being exploited in the wild.",
    )
    await add_item(db_session, feed.id, "g2", title="Phishing wave", summary="Unrelated.")

    research = await add_session(
        db_session,
        title="Weekly triage",
        previews=[f"what do we know about {CVE}?", "thanks"],
    )
    await add_session(db_session, title="Unrelated chat", previews=["hello there"])

    note = await add_note(
        db_session,
        title="Weekly notes",
        body_md=f"## Highlights\n\n{CVE} is the one to watch this week.",
    )
    await add_note(db_session, title="Other notes", body_md="Nothing to see.")
    await db_session.commit()
    return {"item": item.id, "session": research.id, "note": note.id}


# ------------------------------------------------------------------ the headline


async def test_one_query_finds_the_cve_in_all_three_places(client, haystack):
    response = await client.get("/api/search", params={"q": CVE})

    assert response.status_code == 200
    body = response.json()
    assert [hit["id"] for hit in body["items"]] == [haystack["item"]]
    assert [hit["id"] for hit in body["sessions"]] == [haystack["session"]]
    assert [hit["id"] for hit in body["notes"]] == [haystack["note"]]

    for group, expected_type in (("items", "item"), ("sessions", "session"), ("notes", "note")):
        hit = body[group][0]
        assert hit["type"] == expected_type
        assert CVE in hit["snippet"]
        assert hit["title"]
        assert hit["timestamp"] is not None

    assert body["items"][0]["link"] == (
        f"/?q={quote(CVE, safe='')}&item={haystack['item']}&status=all"
    )
    assert body["sessions"][0]["link"] == f"/chat/{haystack['session']}"
    assert body["notes"][0]["link"] == f"/notes/{haystack['note']}"


async def test_search_is_case_insensitive(client, haystack):
    body = (await client.get("/api/search", params={"q": CVE.lower()})).json()

    assert [hit["id"] for hit in body["items"]] == [haystack["item"]]
    assert [hit["id"] for hit in body["sessions"]] == [haystack["session"]]
    assert [hit["id"] for hit in body["notes"]] == [haystack["note"]]


async def test_a_session_with_many_matching_messages_is_one_hit(client, db_session):
    research = await add_session(
        db_session,
        title="Triage",
        previews=[f"first {CVE}", f"second {CVE}", f"third {CVE}"],
    )
    await db_session.commit()

    body = (await client.get("/api/search", params={"q": CVE})).json()

    assert [hit["id"] for hit in body["sessions"]] == [research.id]
    # The snippet comes from the earliest matching message, not the last.
    assert "first" in body["sessions"][0]["snippet"]


async def test_a_session_matching_only_on_title_falls_back_to_the_title(client, db_session):
    research = await add_session(db_session, title=f"{CVE} follow-up", previews=["unrelated talk"])
    await db_session.commit()

    body = (await client.get("/api/search", params={"q": CVE})).json()

    assert [hit["id"] for hit in body["sessions"]] == [research.id]
    assert body["sessions"][0]["snippet"] == f"{CVE} follow-up"


# --------------------------------------------------------------- LIKE escaping


async def test_a_percent_query_matches_only_a_literal_percent(client, db_session):
    literal = await add_note(db_session, title="Budget", body_md="rollout is 100% complete")
    await add_note(db_session, title="Counts", body_md="we saw 1000 hosts affected")
    await db_session.commit()

    body = (await client.get("/api/search", params={"q": "100%"})).json()

    assert [hit["id"] for hit in body["notes"]] == [literal.id]


async def test_an_underscore_query_is_not_a_single_character_wildcard(client, db_session):
    literal = await add_note(db_session, title="Tool", body_md="run a_b to reproduce")
    await add_note(db_session, title="Other", body_md="run axb to reproduce")
    await db_session.commit()

    body = (await client.get("/api/search", params={"q": "a_b"})).json()

    assert [hit["id"] for hit in body["notes"]] == [literal.id]


# ------------------------------------------------------------------ parameters


async def test_types_narrows_the_query_but_keeps_every_key(client, haystack):
    body = (await client.get("/api/search", params={"q": CVE, "types": "notes"})).json()

    assert [hit["id"] for hit in body["notes"]] == [haystack["note"]]
    assert body["items"] == []
    assert body["sessions"] == []


@pytest.mark.parametrize("types", ["items,bogus", "bogus", "  "])
async def test_an_unknown_type_is_a_422(client, types):
    response = await client.get("/api/search", params={"q": CVE, "types": types})

    assert response.status_code == 422


async def test_the_422_names_the_offending_type(client):
    response = await client.get("/api/search", params={"q": CVE, "types": "items,bogus"})

    assert "bogus" in str(response.json()["detail"])


async def test_limit_is_applied_per_type(client, db_session):
    for index in range(5):
        await add_note(db_session, title=f"Note {index}", body_md=f"all about {CVE}")
    await db_session.commit()

    body = (await client.get("/api/search", params={"q": CVE, "limit": 2})).json()

    assert len(body["notes"]) == 2


@pytest.mark.parametrize("limit", [0, 51])
async def test_an_out_of_range_limit_is_a_422(client, limit):
    assert (await client.get("/api/search", params={"q": CVE, "limit": limit})).status_code == 422


@pytest.mark.parametrize("q", ["", " a ", "a"])
async def test_a_too_short_query_is_a_422(client, q):
    assert (await client.get("/api/search", params={"q": q})).status_code == 422


async def test_a_missing_query_is_a_422(client):
    assert (await client.get("/api/search")).status_code == 422


# -------------------------------------------------------------------- archived


async def test_archived_sessions_are_excluded_by_default(client, db_session):
    live = await add_session(db_session, title=f"{CVE} live")
    await add_session(db_session, title=f"{CVE} archived", archived=True)
    await db_session.commit()

    body = (await client.get("/api/search", params={"q": CVE})).json()

    assert [hit["id"] for hit in body["sessions"]] == [live.id]


async def test_archived_sessions_are_included_on_request(client, db_session):
    await add_session(db_session, title=f"{CVE} live")
    await add_session(db_session, title=f"{CVE} archived", archived=True)
    await db_session.commit()

    body = (
        await client.get("/api/search", params={"q": CVE, "include_archived": "true"})
    ).json()

    assert len(body["sessions"]) == 2


# --------------------------------------------------------------------- snippet


async def test_the_snippet_is_centred_on_a_match_deep_inside_a_long_body(client, db_session):
    filler = "lorem ipsum dolor sit amet " * 60
    await add_note(db_session, title="Long", body_md=f"{filler}{CVE} was the cause. {filler}")
    await db_session.commit()

    snippet = (await client.get("/api/search", params={"q": CVE})).json()["notes"][0]["snippet"]

    assert CVE in snippet
    assert snippet.startswith("…")
    assert snippet.endswith("…")
    assert len(snippet) <= search_service.SNIPPET_WIDTH + 2
    # Centred, not merely truncated at the match.
    assert snippet.index(CVE) > 30
    assert "was the cause" in snippet


async def test_the_snippet_collapses_newlines_and_runs_of_spaces(client, db_session):
    await add_note(db_session, title="Messy", body_md=f"## Heading\n\n\n{CVE}   was    noisy")
    await db_session.commit()

    snippet = (await client.get("/api/search", params={"q": CVE})).json()["notes"][0]["snippet"]

    assert "\n" not in snippet
    assert "  " not in snippet
    assert f"{CVE} was noisy" in snippet


def test_make_snippet_without_a_match_returns_the_head_of_the_text():
    snippet = search_service.make_snippet("a" * 400, "zzz", width=50)

    assert snippet == "a" * 50 + "…"


def test_make_snippet_of_missing_text_is_empty():
    assert search_service.make_snippet(None, CVE) == ""
    assert search_service.make_snippet("", CVE) == ""


def test_make_snippet_does_not_pad_a_short_text():
    assert search_service.make_snippet("  short  text ", "text") == "short text"


# ---------------------------------------------------------------------- order


async def test_items_come_back_newest_published_first(client, db_session):
    from datetime import timedelta

    feed = await add_feed(db_session)
    now = utcnow()
    older = await add_item(
        db_session, feed.id, "old", title=f"{CVE} old", published_at=now - timedelta(days=2)
    )
    newer = await add_item(db_session, feed.id, "new", title=f"{CVE} new", published_at=now)
    await db_session.commit()

    body = (await client.get("/api/search", params={"q": CVE})).json()

    assert [hit["id"] for hit in body["items"]] == [newer.id, older.id]


async def test_dismissed_items_are_still_history(client, db_session):
    feed = await add_feed(db_session)
    dismissed = await add_item(
        db_session, feed.id, "d1", title=f"{CVE} advisory", status="dismissed"
    )
    await db_session.commit()

    body = (await client.get("/api/search", params={"q": CVE})).json()

    assert [hit["id"] for hit in body["items"]] == [dismissed.id]
