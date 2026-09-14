"""Notes CRUD, search, pagination and the Markdown export.

Nothing here streams or touches the Anthropic API — the generation endpoint has
its own file. These are the plain REST endpoints the Notes page reads and writes.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.db.models import Feed, FeedItem, Note, NoteSource, utcnow


@pytest.fixture
async def seeded_item(db_session) -> FeedItem:
    feed = Feed(url="https://example.test/rss", title="Example Security")
    db_session.add(feed)
    await db_session.flush()
    item = FeedItem(
        feed_id=feed.id,
        guid="g1",
        url="https://example.test/acme-rce",
        title="AcmeVPN pre-auth RCE",
        summary="A pre-auth RCE in AcmeVPN.",
        published_at=utcnow(),
    )
    db_session.add(item)
    await db_session.commit()
    return item


async def make_note(
    session_factory,
    *,
    title: str = "Weekly notes",
    body: str = "## AcmeVPN\n**What happened** — a thing.",
    created_at=None,
    sources: list[dict] | None = None,
) -> int:
    async with session_factory() as session:
        note = Note(title=title, body_md=body, template_used="tpl")
        if created_at is not None:
            note.created_at = created_at
            note.updated_at = created_at
        session.add(note)
        await session.flush()
        for source in sources or []:
            session.add(NoteSource(note_id=note.id, **source))
        await session.commit()
        return note.id


# ------------------------------------------------------------------ listing


async def test_list_is_newest_first_with_counts_and_excerpt(client, session_factory):
    base = utcnow()
    older = await make_note(
        session_factory,
        title="Older",
        body="# x\n" + "a" * 500,
        created_at=base - timedelta(days=1),
    )
    newer = await make_note(
        session_factory,
        title="Newer",
        created_at=base,
        sources=[
            {"feed_item_id": None, "url": "https://a.test/1", "title": "One"},
            {"feed_item_id": None, "url": "https://a.test/2", "title": "Two"},
        ],
    )

    body = (await client.get("/api/notes")).json()

    assert [row["id"] for row in body["notes"]] == [newer, older]
    assert body["next_cursor"] is None
    assert body["notes"][0]["source_count"] == 2
    assert body["notes"][1]["source_count"] == 0
    # Markers are kept verbatim, and the excerpt is capped.
    assert body["notes"][1]["excerpt"].startswith("# x\n")
    assert len(body["notes"][1]["excerpt"]) <= 200


async def test_list_search_matches_title_and_body_but_not_others(client, session_factory):
    in_title = await make_note(session_factory, title="Log4Shell redux", body="nothing special")
    in_body = await make_note(session_factory, title="Weekly", body="a note about log4shell")
    await make_note(session_factory, title="Phishing wave", body="unrelated")

    found = (await client.get("/api/notes?q=log4shell")).json()["notes"]

    assert sorted(row["id"] for row in found) == sorted([in_title, in_body])


async def test_list_search_treats_wildcards_literally(client, session_factory):
    match = await make_note(session_factory, title="100% coverage", body="x")
    await make_note(session_factory, title="nothing", body="y")

    found = (await client.get("/api/notes?q=100%25")).json()["notes"]

    assert [row["id"] for row in found] == [match]


async def test_keyset_pagination_walks_every_note_exactly_once(client, session_factory):
    base = utcnow()
    created = [
        await make_note(
            session_factory, title=f"Note {index}", created_at=base - timedelta(minutes=index)
        )
        for index in range(55)
    ]

    seen: list[int] = []
    cursor: str | None = None
    pages = 0
    while True:
        query = "/api/notes?limit=20" + (f"&cursor={cursor}" if cursor else "")
        page = (await client.get(query)).json()
        seen.extend(row["id"] for row in page["notes"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert pages < 10

    assert sorted(seen) == sorted(created)
    assert len(seen) == len(set(seen))


async def test_list_rejects_a_malformed_cursor(client):
    assert (await client.get("/api/notes?cursor=not-a-cursor")).status_code == 422


# ------------------------------------------------------------------ get one


async def test_get_one_includes_sources(client, session_factory, seeded_item):
    note_id = await make_note(
        session_factory,
        sources=[
            {
                "feed_item_id": seeded_item.id,
                "url": seeded_item.url,
                "title": seeded_item.title,
            },
            {"feed_item_id": None, "url": "https://vendor.test/advisory", "title": None},
        ],
    )

    body = (await client.get(f"/api/notes/{note_id}")).json()

    assert body["id"] == note_id
    assert body["body_md"].startswith("## AcmeVPN")
    assert body["template_used"] == "tpl"
    assert [source["feed_item_id"] for source in body["sources"]] == [seeded_item.id, None]
    assert body["sources"][1]["url"] == "https://vendor.test/advisory"


async def test_get_unknown_note_is_a_404(client):
    assert (await client.get("/api/notes/9999")).status_code == 404


# ------------------------------------------------------------------- patch


async def test_patch_title_only_leaves_the_body_and_bumps_updated_at(client, session_factory):
    note_id = await make_note(session_factory, created_at=utcnow() - timedelta(hours=1))
    before = (await client.get(f"/api/notes/{note_id}")).json()

    patched = await client.patch(f"/api/notes/{note_id}", json={"title": "Renamed"})

    assert patched.status_code == 200
    body = patched.json()
    assert body["title"] == "Renamed"
    assert body["body_md"] == before["body_md"]
    assert body["updated_at"] > before["updated_at"]
    assert body["created_at"] == before["created_at"]


async def test_patch_body_only_leaves_the_title(client, session_factory):
    note_id = await make_note(session_factory)

    body = (await client.patch(f"/api/notes/{note_id}", json={"body_md": "# new"})).json()

    assert body["body_md"] == "# new"
    assert body["title"] == "Weekly notes"


async def test_patch_both_fields(client, session_factory):
    note_id = await make_note(session_factory)

    body = (
        await client.patch(f"/api/notes/{note_id}", json={"title": "T", "body_md": "B"})
    ).json()

    assert (body["title"], body["body_md"]) == ("T", "B")


async def test_patch_rejects_an_empty_payload_and_an_empty_body(client, session_factory):
    note_id = await make_note(session_factory)

    assert (await client.patch(f"/api/notes/{note_id}", json={})).status_code == 422
    assert (await client.patch(f"/api/notes/{note_id}", json={"body_md": ""})).status_code == 422


async def test_patch_unknown_note_is_a_404(client):
    assert (await client.patch("/api/notes/9999", json={"title": "x"})).status_code == 404


# ------------------------------------------------------------------ delete


async def test_delete_removes_the_note_and_its_sources(client, session_factory, seeded_item):
    note_id = await make_note(
        session_factory,
        sources=[{"feed_item_id": seeded_item.id, "url": seeded_item.url, "title": "t"}],
    )

    assert (await client.delete(f"/api/notes/{note_id}")).status_code == 204
    assert (await client.get(f"/api/notes/{note_id}")).status_code == 404

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(NoteSource)) == 0


async def test_delete_unknown_note_is_a_404(client):
    assert (await client.delete("/api/notes/9999")).status_code == 404


# ------------------------------------------------------------------ export


async def test_export_is_the_body_byte_for_byte_with_a_sane_filename(client, session_factory):
    body = "## Résumé: “quotes” & <angles>\n\n- one\n- two\n"
    note_id = await make_note(
        session_factory, title='Weekly "notes": Räum/Ünicode — 2026?', body=body
    )

    response = await client.get(f"/api/notes/{note_id}/export.md")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment; ")
    assert disposition.endswith('.md"')
    # The filename is plain ASCII and carries nothing that could break the header.
    filename = disposition.split('filename="', 1)[1].rstrip('"')
    assert filename.isascii()
    assert not any(character in filename for character in '"\r\n;/\\')
    assert filename.startswith(f"note-{note_id}-")
    # ...while the body itself is untouched.
    assert response.text == body


async def test_export_falls_back_to_a_bare_slug_for_an_unsluggable_title(client, session_factory):
    note_id = await make_note(session_factory, title="——", body="x")

    response = await client.get(f"/api/notes/{note_id}/export.md")

    assert f'filename="note-{note_id}.md"' in response.headers["content-disposition"]


async def test_export_unknown_note_is_a_404(client):
    assert (await client.get("/api/notes/9999/export.md")).status_code == 404


# ------------------------------------------------------------------- cancel


async def test_cancel_an_unknown_generation_is_a_200(client):
    response = await client.post("/api/notes/generate/cancel", json={"generation_id": "nope"})

    assert response.status_code == 200
    assert response.json() == {"cancelled": False}
