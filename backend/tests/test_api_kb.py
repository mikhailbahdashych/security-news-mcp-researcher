"""The ``/api/kb`` contract, and the capture triggers that feed it.

Every fetch the knowledge base makes goes through an ``httpx2.MockTransport``
handed to the service by a dependency override — the same seam the routes use in
production, so nothing here reaches the network and nothing here is a stub of the
code under test.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx2
import pytest
from feed_fixtures import fixture_text, routes_transport
from sqlalchemy import func, select

from app.api.deps import get_kb_service
from app.db.models import Feed, FeedItem, Note, utcnow
from app.kb.capture import capture_article
from app.kb.models import KbActivity, KbEntry, KbEntryTopic, Topic
from app.kb.retrieval import Hit
from app.kb.service import EntryFacts, KbService
from app.schemas.kb import HitRead

ARTICLE_URL = "https://example.test/article"

BODY = (
    "# The xz backdoor\n\n"
    "A malicious commit in liblzma introduced a backdoor tracked as CVE-2024-3094. "
    "The payload hooks RSA_public_decrypt through the IFUNC resolver, which is why "
    "it only activates inside an sshd process linked against systemd's notification "
    "library rather than in liblzma itself.\n\n"
    "Debian and Fedora shipped the affected build in their unstable channels only, "
    "and no stable release ever carried it. Downgrading liblzma to 5.4.6 removes the "
    "payload entirely, and the distributions have published rebuilt packages that "
    "reverse the two malicious release tarballs.\n"
)


@pytest.fixture
def article_transport():
    return routes_transport({ARTICLE_URL: httpx2.Response(200, text=fixture_text("article.html"))})


@pytest.fixture
def kb(app, session_factory, article_transport):
    """Wire the app's knowledge base to the test database and a mock transport."""
    service = KbService(session_factory=session_factory, transport=article_transport)
    app.dependency_overrides[get_kb_service] = lambda: service
    return service


@pytest.fixture
async def item(db_session) -> FeedItem:
    feed = Feed(url="https://example.test/feed.xml", title="Example Feed")
    db_session.add(feed)
    await db_session.flush()
    row = FeedItem(
        feed_id=feed.id,
        guid="xz-1",
        title="The xz backdoor",
        url="https://example.test/xz",
        summary="A backdoor in liblzma.",
        content_text=BODY,
        published_at=datetime(2024, 3, 29, 12, 0, 0),
    )
    db_session.add(row)
    await db_session.commit()
    return row


async def _seed(kb, *, title, url, text=BODY, published_at=None):
    """An entry in the database without going through a route."""
    return await capture_article(
        kb.session_factory,
        kb.embedder,
        url=url,
        title=title,
        text=text,
        published_at=published_at,
        captured_by="user",
    )


async def _entry_from_item(client, kb, item) -> dict:
    response = await client.post("/api/kb/entries", json={"feed_item_id": item.id})
    assert response.status_code == 201, response.text
    return response.json()


# ------------------------------------------------------------ POST /entries


async def test_saving_a_feed_item_creates_an_entry(client, kb, item):
    response = await client.post("/api/kb/entries", json={"feed_item_id": item.id})

    assert response.status_code == 201
    body = response.json()
    assert body["kind"] == "article"
    assert body["authorship"] == "source"
    assert body["captured_by"] == "user"
    assert body["review_status"] == "unreviewed"
    assert body["url"] == "https://example.test/xz"
    assert body["source_name"] == "Example Feed"
    assert body["published_at"].startswith("2024-03-29")
    assert body["links"] == {"feed_item_id": item.id, "note_id": None, "session_id": None}
    assert body["entities"] == [{"kind": "cve", "value": "CVE-2024-3094", "source": "regex"}]
    assert body["snapshot_version"] == 1
    assert body["snapshot_chars"] > 0
    assert body["chunks"] >= 1
    assert body["pending_chunks"] == body["chunks"]


async def test_saving_the_same_item_twice_answers_200_with_the_existing_entry(client, kb, item):
    first = await _entry_from_item(client, kb, item)

    response = await client.post("/api/kb/entries", json={"feed_item_id": item.id})

    assert response.status_code == 200
    assert response.json()["id"] == first["id"]


async def test_saving_a_url_fetches_it_through_the_extractor(client, kb):
    response = await client.post("/api/kb/entries", json={"url": ARTICLE_URL})

    assert response.status_code == 201
    assert response.json()["url"] == ARTICLE_URL


async def test_saving_needs_exactly_one_of_a_url_and_an_item(client, kb, item):
    assert (await client.post("/api/kb/entries", json={})).status_code == 422
    both = await client.post("/api/kb/entries", json={"url": ARTICLE_URL, "feed_item_id": item.id})
    assert both.status_code == 422


async def test_text_below_the_minimum_is_a_409_with_a_reason(client, kb, db_session, item):
    item.content_text = "far too short"
    item.summary = "also short"
    await db_session.commit()

    response = await client.post("/api/kb/entries", json={"feed_item_id": item.id})

    assert response.status_code == 409
    assert "minimum" in response.json()["detail"]
    assert await db_session.scalar(select(func.count()).select_from(KbEntry)) == 0


# ------------------------------------------------------------- GET /entries


async def test_the_entries_list_is_newest_first_and_pages_by_cursor(client, kb, db_session):
    for day in (1, 3, 2):
        await _seed(
            kb,
            title=f"Advisory {day}",
            url=f"https://example.test/a{day}",
            text=BODY,
            published_at=datetime(2024, 3, day),
        )

    response = await client.get("/api/kb/entries", params={"limit": 2})

    body = response.json()
    assert [entry["title"] for entry in body["entries"]] == ["Advisory 3", "Advisory 2"]
    assert body["next_cursor"]
    assert "hits" not in body

    page_two = await client.get(
        "/api/kb/entries", params={"limit": 2, "cursor": body["next_cursor"]}
    )
    assert [entry["title"] for entry in page_two.json()["entries"]] == ["Advisory 1"]
    assert page_two.json()["next_cursor"] is None


async def test_the_entries_list_filters_by_kind_review_since_topic_and_entity(
    client, kb, db_session
):
    first = await _seed(
        kb, title="Recent", url="https://example.test/recent", text=BODY, published_at=utcnow()
    )
    old = await _seed(
        kb,
        title="Ancient",
        url="https://example.test/old",
        text=BODY.replace("CVE-2024-3094", "CVE-2019-0708"),
        published_at=utcnow() - timedelta(days=400),
    )
    topic = Topic(name="supply chain")
    db_session.add(topic)
    await db_session.flush()
    db_session.add(KbEntryTopic(entry_id=first.entry_id, topic_id=topic.id))
    entry = await db_session.get(KbEntry, old.entry_id)
    entry.review_status = "reviewed"
    await db_session.commit()

    async def ids(**params) -> list[int]:
        response = await client.get("/api/kb/entries", params=params)
        assert response.status_code == 200, response.text
        return [entry["id"] for entry in response.json()["entries"]]

    assert await ids(kind="article") == [first.entry_id, old.entry_id]
    assert await ids(kind="note") == []
    assert await ids(review="reviewed") == [old.entry_id]
    assert await ids(topic_id=topic.id) == [first.entry_id]
    assert await ids(entity="cve:CVE-2019-0708") == [old.entry_id]
    assert await ids(since=(utcnow() - timedelta(days=7)).isoformat()) == [first.entry_id]


async def test_a_query_string_returns_hits_rather_than_entries(client, kb):
    saved = await _seed(kb, title="The xz backdoor", url="https://example.test/xz", text=BODY)

    response = await client.get("/api/kb/entries", params={"q": "liblzma"})

    body = response.json()
    assert "entries" not in body
    assert [hit["entry"]["id"] for hit in body["hits"]] == [saved.entry_id]
    assert body["hits"][0]["snippet"]
    assert body["hits"][0]["matched_by"] == "keyword"


# ------------------------------------------- detail, patch, delete, refresh


async def test_the_detail_view_carries_the_snapshot_versions_and_activity(client, kb, item):
    created = await _entry_from_item(client, kb, item)

    response = await client.get(f"/api/kb/entries/{created['id']}")

    body = response.json()
    assert body["snapshot_md"].startswith("# The xz backdoor")
    assert [version["version"] for version in body["versions"]] == [1]
    assert body["versions"][0]["sha256"]
    assert [row["action"] for row in body["activity"]] == ["capture"]


async def test_an_unknown_entry_is_a_404(client, kb):
    assert (await client.get("/api/kb/entries/999")).status_code == 404


async def test_patching_an_entry_stores_the_notes_and_the_review_status(client, kb, item):
    created = await _entry_from_item(client, kb, item)

    response = await client.patch(
        f"/api/kb/entries/{created['id']}",
        json={"notes_md": "Ask the platform team.", "review_status": "reviewed"},
    )

    assert response.status_code == 200
    assert response.json()["notes_md"] == "Ask the platform team."
    assert response.json()["review_status"] == "reviewed"
    reread = await client.get(f"/api/kb/entries/{created['id']}")
    assert reread.json()["notes_md"] == "Ask the platform team."


async def test_delete_and_undelete_round_trip(client, kb, item):
    created = await _entry_from_item(client, kb, item)

    deleted = await client.post(f"/api/kb/entries/{created['id']}/delete")
    assert deleted.status_code == 200
    assert deleted.json()["deleted_at"] is not None
    assert (await client.get("/api/kb/entries")).json()["entries"] == []
    trash = await client.get("/api/kb/entries", params={"deleted": "true"})
    assert [entry["id"] for entry in trash.json()["entries"]] == [created["id"]]

    restored = await client.post(f"/api/kb/entries/{created['id']}/undelete")
    assert restored.status_code == 200
    assert restored.json()["deleted_at"] is None
    assert [entry["id"] for entry in (await client.get("/api/kb/entries")).json()["entries"]] == [
        created["id"]
    ]


async def test_purge_refuses_an_entry_that_is_not_deleted(client, kb, item):
    created = await _entry_from_item(client, kb, item)

    refused = await client.post("/api/kb/purge", json={"ids": [created["id"]]})
    assert refused.status_code == 409

    await client.post(f"/api/kb/entries/{created['id']}/delete")
    purged = await client.post("/api/kb/purge", json={"ids": [created["id"]]})
    assert purged.status_code == 200
    assert purged.json() == {"purged": 1}


async def test_refresh_reports_whether_the_text_moved(client, kb):
    created = await client.post("/api/kb/entries", json={"url": ARTICLE_URL})

    response = await client.post(f"/api/kb/entries/{created.json()['id']}/refresh")

    body = response.json()
    assert body["changed"] is False
    assert body["version"] == 1
    assert body["entry"]["id"] == created.json()["id"]


async def test_merge_keeps_the_older_entry(client, kb):
    older = await _seed(kb, title="Older", url="https://example.test/older", text=BODY)
    newer = await _seed(
        kb, title="Newer", url="https://example.test/newer", text=BODY + "\n\nAn extra paragraph."
    )

    response = await client.post(
        f"/api/kb/entries/{newer.entry_id}/merge", json={"into": older.entry_id}
    )

    assert response.status_code == 200
    assert response.json()["id"] == older.entry_id
    assert response.json()["deleted_at"] is None


# --------------------------------------------------- search, stats, activity


async def test_search_returns_hits_and_names_its_mode(client, kb):
    saved = await _seed(kb, title="The xz backdoor", url="https://example.test/xz", text=BODY)

    response = await client.post("/api/kb/search", json={"q": "CVE-2024-3094"})

    body = response.json()
    assert body["mode"] == "keyword"
    assert [hit["entry"]["id"] for hit in body["hits"]] == [saved.entry_id]
    # An exact CVE lookup is the exact leg, ahead of both query legs.
    assert body["hits"][0]["matched_by"] == "entity"


async def test_stats_report_the_index_and_the_counts(client, kb, item):
    await _entry_from_item(client, kb, item)

    body = (await client.get("/api/kb/stats")).json()

    assert body["entries"] == 1
    assert body["deleted"] == 0
    assert body["chunks"] >= 1
    assert body["pending_chunks"] == body["chunks"]
    assert body["entities"] == 1
    assert body["embeddings_configured"] is False
    assert body["index"]["fts5"] is True
    assert body["index"]["vec_version"]
    assert body["index"]["outdated"] is False
    assert body["index"]["reasons"] == []


async def test_the_activity_endpoint_lists_the_trail_newest_first(client, kb, item):
    created = await _entry_from_item(client, kb, item)
    await client.post(f"/api/kb/entries/{created['id']}/delete")

    body = (await client.get("/api/kb/activity")).json()

    assert [row["action"] for row in body["items"]] == ["delete", "capture"]
    assert body["items"][0]["entry_id"] == created["id"]


def test_matched_by_passes_through_without_translation(db_session):
    """``retrieval`` and the wire share one vocabulary — no mapping layer.

    A both-leg hit cannot arise in Phase 1 (``NullEmbedder`` means no vector leg),
    so it is asserted directly: a mapping that silently fell back to ``keyword``
    would otherwise go unnoticed until Phase 2.
    """
    entry = KbEntry(
        id=1,
        kind="article",
        title="Both legs",
        authorship="source",
        review_status="unreviewed",
        captured_by="user",
        notes_md="",
        captured_at=utcnow(),
        updated_at=utcnow(),
    )
    hit = Hit(
        entry=entry,
        chunk=None,
        snippet="s",
        distance=0.1,
        bm25=-1.0,
        score=0.5,
        matched_by="both",
    )

    assert HitRead.from_hit(hit, EntryFacts()).model_dump()["matched_by"] == "both"


# -------------------------------------------------------- topics and tags


async def test_topics_can_be_created_listed_renamed_and_deleted(client, kb):
    created = await client.post("/api/kb/topics", json={"name": "supply chain"})
    assert created.status_code == 201
    topic_id = created.json()["id"]

    listed = await client.get("/api/kb/topics")
    assert [topic["name"] for topic in listed.json()] == ["supply chain"]
    assert listed.json()[0]["entry_count"] == 0

    renamed = await client.patch(f"/api/kb/topics/{topic_id}", json={"name": "supply-chain"})
    assert renamed.json()["name"] == "supply-chain"

    assert (await client.delete(f"/api/kb/topics/{topic_id}")).status_code == 204
    assert (await client.get("/api/kb/topics")).json() == []


async def test_setting_an_entrys_topics_replaces_the_set_and_confirms_them(client, kb, item):
    created = await _entry_from_item(client, kb, item)
    first = (await client.post("/api/kb/topics", json={"name": "one"})).json()
    second = (await client.post("/api/kb/topics", json={"name": "two"})).json()

    await client.post(f"/api/kb/entries/{created['id']}/topics", json={"topic_ids": [first["id"]]})
    response = await client.post(
        f"/api/kb/entries/{created['id']}/topics", json={"topic_ids": [second["id"]]}
    )

    assert response.json()["topics"] == [
        {"id": second["id"], "name": "two", "color": None, "suggested": False}
    ]


async def test_setting_tags_replaces_them(client, kb, item):
    created = await _entry_from_item(client, kb, item)

    response = await client.post(
        f"/api/kb/entries/{created['id']}/tags", json={"tags": ["xz", "supply-chain", "xz"]}
    )

    assert response.json()["tags"] == [
        {"tag": "supply-chain", "suggested": False},
        {"tag": "xz", "suggested": False},
    ]


# ------------------------------------------------------------- the triggers


async def test_starring_an_item_captures_it(client, kb, item, db_session):
    response = await client.patch(f"/api/items/{item.id}", json={"status": "starred"})

    assert response.status_code == 200
    entries = (await client.get("/api/kb/entries")).json()["entries"]
    assert [entry["links"]["feed_item_id"] for entry in entries] == [item.id]
    assert entries[0]["captured_by"] == "auto"


async def test_dismissing_an_item_captures_nothing(client, kb, item):
    await client.patch(f"/api/items/{item.id}", json={"status": "dismissed"})

    assert (await client.get("/api/kb/entries")).json()["entries"] == []


async def test_the_star_trigger_respects_the_policy_setting(client, kb, item):
    await client.put("/api/settings", json={"kb_capture_starred": False})

    await client.patch(f"/api/items/{item.id}", json={"status": "starred"})

    assert (await client.get("/api/kb/entries")).json()["entries"] == []


async def test_a_capture_failure_never_fails_the_star(
    client, app, session_factory, item, db_session
):
    class BrokenService(KbService):
        async def capture_feed_item(self, *args, **kwargs):
            raise RuntimeError("the extractor exploded")

    app.dependency_overrides[get_kb_service] = lambda: BrokenService(
        session_factory=session_factory
    )

    response = await client.patch(f"/api/items/{item.id}", json={"status": "starred"})

    assert response.status_code == 200
    assert response.json()["status"] == "starred"
    await db_session.refresh(item)
    assert item.status == "starred"
    rows = (await db_session.execute(select(KbActivity))).scalars().all()
    assert [row.action for row in rows] == ["skip"]
    assert "the extractor exploded" in rows[0].detail


async def test_saving_a_note_captures_it(client, kb, db_session):
    note = Note(title="Week 12", body_md=BODY, template_used="t")
    db_session.add(note)
    await db_session.commit()

    await client.patch(f"/api/notes/{note.id}", json={"body_md": BODY + "\n\nOne more line."})

    entries = (await client.get("/api/kb/entries")).json()["entries"]
    assert [entry["kind"] for entry in entries] == ["note"]
    assert entries[0]["authorship"] == "human"
    assert entries[0]["links"]["note_id"] == note.id


async def test_the_note_trigger_respects_the_policy_setting(client, kb, db_session):
    note = Note(title="Week 12", body_md=BODY, template_used="t")
    db_session.add(note)
    await db_session.commit()
    await client.put("/api/settings", json={"kb_capture_notes": False})

    await client.patch(f"/api/notes/{note.id}", json={"body_md": BODY + "\n\nEdited."})

    assert (await client.get("/api/kb/entries")).json()["entries"] == []


# ---------------------------------------------------------------- settings


async def test_the_settings_endpoint_exposes_the_capture_policy(client):
    body = (await client.get("/api/settings")).json()

    assert body["kb_capture_starred"] is True
    assert body["kb_capture_notes"] is True
    assert body["kb_min_snapshot_chars"] == 400
    assert body["kb_schema_version"]["vec_dimensions"] == 1024
    assert body["kb_schema_version"]["tokenizer"] == "unicode61 remove_diacritics 2"

    updated = await client.put(
        "/api/settings", json={"kb_capture_notes": False, "kb_min_snapshot_chars": 200}
    )
    assert updated.json()["kb_capture_notes"] is False
    assert updated.json()["kb_min_snapshot_chars"] == 200


# ------------------------------------------------- regressions, fix round 1


async def test_the_search_branch_applies_the_review_filter(client, kb, db_session):
    """``review`` is a contract filter; under ``q`` it used to do nothing.

    ``reviewed_only=review == "reviewed"`` turned ``review=unreviewed`` into "no
    filter at all" — the user set a narrowing and got everything back.
    """
    reviewed = await _seed(kb, title="Reviewed", url="https://example.test/reviewed")
    unreviewed = await _seed(kb, title="Unreviewed", url="https://example.test/unreviewed")
    entry = await db_session.get(KbEntry, reviewed.entry_id)
    entry.review_status = "reviewed"
    await db_session.commit()

    async def ids(**params) -> list[int]:
        response = await client.get("/api/kb/entries", params={"q": "liblzma", **params})
        assert response.status_code == 200, response.text
        return sorted(hit["entry"]["id"] for hit in response.json()["hits"])

    assert await ids(review="reviewed") == [reviewed.entry_id]
    assert await ids(review="unreviewed") == [unreviewed.entry_id]
    assert await ids() == sorted([reviewed.entry_id, unreviewed.entry_id])


async def test_the_search_branch_honours_the_deleted_view(client, kb, item):
    live = await _entry_from_item(client, kb, item)

    hits = await client.get("/api/kb/entries", params={"q": "liblzma"})
    trash = await client.get("/api/kb/entries", params={"q": "liblzma", "deleted": "true"})

    assert [hit["entry"]["id"] for hit in hits.json()["hits"]] == [live["id"]]
    # A soft delete drops the chunks, so a deleted entry is not searchable at
    # all: the trash is a list, never a search, and saying so beats quietly
    # answering with the live matches.
    assert trash.json()["hits"] == []
    assert "next_cursor" not in trash.json()
    assert "next_cursor" not in hits.json()


async def test_a_duplicate_topic_name_is_a_409_not_a_500(client, kb):
    assert (await client.post("/api/kb/topics", json={"name": "Ransomware"})).status_code == 201

    clash = await client.post("/api/kb/topics", json={"name": "Ransomware"})

    assert clash.status_code == 409
    assert "Ransomware" in clash.json()["detail"]


async def test_renaming_a_topic_onto_an_existing_name_is_a_409(client, kb):
    first = (await client.post("/api/kb/topics", json={"name": "one"})).json()
    await client.post("/api/kb/topics", json={"name": "two"})

    clash = await client.patch(f"/api/kb/topics/{first['id']}", json={"name": "two"})

    assert clash.status_code == 409
    assert "two" in clash.json()["detail"]


async def test_saving_a_deleted_entry_again_brings_it_back(client, kb, item):
    """Save means "I want this", so a re-save of something deleted revives it.

    It used to answer 200 with a soft-deleted entry: the client said "Saved",
    the timeline did not list it, and only the trash view could find it again.
    """
    created = await _entry_from_item(client, kb, item)
    await client.post(f"/api/kb/entries/{created['id']}/delete")

    again = await client.post("/api/kb/entries", json={"feed_item_id": item.id})

    assert again.status_code == 200
    assert again.json()["id"] == created["id"]
    assert again.json()["deleted_at"] is None
    assert again.json()["chunks"] >= 1
    listed = (await client.get("/api/kb/entries")).json()["entries"]
    assert [entry["id"] for entry in listed] == [created["id"]]


async def test_restarring_a_deleted_item_brings_its_entry_back(client, kb, item):
    created = await _entry_from_item(client, kb, item)
    await client.post(f"/api/kb/entries/{created['id']}/delete")

    await client.patch(f"/api/items/{item.id}", json={"status": "starred"})

    listed = (await client.get("/api/kb/entries")).json()["entries"]
    assert [entry["id"] for entry in listed] == [created["id"]]


async def test_a_url_that_is_not_http_is_a_422(client, kb):
    response = await client.post("/api/kb/entries", json={"url": "javascript:alert(1)"})

    assert response.status_code == 422
    assert "http" in response.json()["detail"]


async def test_a_fetch_failure_is_a_502(client, kb):
    """Three different failures used to share one status code.

    "You typed it wrong", "the site was down" and "the page was too short" are
    different things to a client, and 409 could only say the last of them.
    """
    response = await client.post("/api/kb/entries", json={"url": "https://example.test/missing"})

    assert response.status_code == 502
    assert response.json()["detail"]
