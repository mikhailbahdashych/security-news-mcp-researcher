"""Capture: snapshots, dedup, entities, refresh, soft delete, merge.

Everything here runs against a real SQLite file with FTS5 and vec0 loaded, and
every fetch goes through ``httpx2.MockTransport``. The embedder is
``NullEmbedder`` unless a test is specifically about embedding, because Phase 1
ships no other one and "chunks stay pending" is the normal state.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timedelta

import httpx2
import pytest
from fakes.embedder import FakeEmbedder
from feed_fixtures import fixture_text, routes_transport
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Feed, FeedItem, Note, utcnow
from app.kb import capture as capture_module
from app.kb.capture import (
    DEFAULT_MIN_SNAPSHOT_CHARS,
    KbConflict,
    RefreshResult,
    capture_article,
    capture_note,
    capture_url,
    content_hash,
    merge_entries,
    purge,
    refresh_snapshot,
    soft_delete,
    undelete,
)
from app.kb.embeddings import EmbeddingError, NullEmbedder
from app.kb.models import (
    KbActivity,
    KbChunk,
    KbEntry,
    KbEntryEntity,
    KbEntryLink,
    KbEntryTag,
    KbEntryTopic,
    KbSnapshot,
    Topic,
)
from app.kb.retrieval import hybrid_search
from app.kb.schema import VEC_DIMENSIONS
from app.kb.service import KbService
from app.kb.store import SqliteKnowledgeStore
from app.kb.urls import canonical_url
from app.services import settings as settings_service

ARTICLE = (
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


async def _search(session_factory, query: str, **kwargs):
    store = SqliteKnowledgeStore(session_factory)
    return await hybrid_search(store, NullEmbedder(), query, **kwargs)


async def _capture(session_factory, **overrides):
    values = {
        "url": "https://example.test/xz",
        "title": "The xz backdoor",
        "source_name": "Example",
        "text": ARTICLE,
        "published_at": None,
        "feed_item_id": None,
        "captured_by": "user",
    }
    values.update(overrides)
    return await capture_article(session_factory, NullEmbedder(), **values)


async def _feed_item(db_session, **overrides) -> FeedItem:
    feed = Feed(url="https://example.test/feed.xml", title="Example")
    db_session.add(feed)
    await db_session.flush()
    values = {
        "feed_id": feed.id,
        "guid": "xz-1",
        "title": "The xz backdoor",
        "url": "https://example.test/xz",
        "summary": "A backdoor in liblzma.",
        "content_text": ARTICLE,
        "published_at": datetime(2024, 3, 29, 12, 0, 0),
    }
    values.update(overrides)
    item = FeedItem(**values)
    db_session.add(item)
    await db_session.commit()
    return item


# --------------------------------------------------------------------- pure


def test_canonical_url_strips_tracking_parameters_and_the_fragment():
    assert (
        canonical_url("HTTPS://Example.test:443/xz?utm_source=rss&id=7#top")
        == "https://example.test/xz?id=7"
    )


def test_canonical_url_refuses_anything_that_is_not_http():
    assert canonical_url("javascript:alert(1)") is None
    assert canonical_url("file:///etc/passwd") is None
    assert canonical_url("") is None
    assert canonical_url(None) is None


def test_refresh_status_names_the_three_outcomes():
    """A failed re-read and an unchanged one are not the same answer.

    Both are ``changed=False``, and reporting them alike told the user a
    Cloudflare block was "the source has not moved" — the opposite of the truth.
    """
    assert RefreshResult(entry_id=1, changed=True, version=2).status == "updated"
    assert RefreshResult(entry_id=1, changed=False, version=1).status == "unchanged"
    assert RefreshResult(entry_id=1, changed=False, version=1, reason="HTTP 403").status == "failed"


def test_content_hash_ignores_case_and_whitespace():
    assert content_hash("The  xz\nbackdoor ") == content_hash("the xz backdoor")
    assert content_hash("the xz backdoor") != content_hash("the xz backdoors")


# ------------------------------------------------------------------ capture


async def test_capture_writes_an_entry_a_first_snapshot_chunks_and_entities(
    session_factory, db_session
):
    result = await _capture(session_factory)

    assert result.created is True
    assert result.skipped_reason is None
    entry = await db_session.get(KbEntry, result.entry_id)
    assert entry.kind == "article"
    assert entry.authorship == "source"
    assert entry.review_status == "unreviewed"
    assert entry.content_hash == content_hash(ARTICLE)

    snapshots = (
        (await db_session.execute(select(KbSnapshot).where(KbSnapshot.entry_id == entry.id)))
        .scalars()
        .all()
    )
    assert [snapshot.version for snapshot in snapshots] == [1]
    assert entry.current_snapshot_id == snapshots[0].id
    assert snapshots[0].chars == len(ARTICLE.strip())

    chunks = (
        (await db_session.execute(select(KbChunk).where(KbChunk.entry_id == entry.id)))
        .scalars()
        .all()
    )
    assert chunks and all(chunk.snapshot_id == snapshots[0].id for chunk in chunks)
    # The FTS content-sync trigger fired for every one of them.
    indexed = await db_session.scalar(text("SELECT count(*) FROM kb_chunks_fts"))
    assert indexed == len(chunks)

    entities = (
        (await db_session.execute(select(KbEntryEntity).where(KbEntryEntity.entry_id == entry.id)))
        .scalars()
        .all()
    )
    assert [(row.kind, row.value, row.source) for row in entities] == [
        ("cve", "CVE-2024-3094", "regex")
    ]


async def test_capture_leaves_chunks_pending_without_an_embedder(session_factory, db_session):
    result = await _capture(session_factory)

    pending = await db_session.scalar(
        select(func.count())
        .select_from(KbChunk)
        .where(KbChunk.entry_id == result.entry_id, KbChunk.embedded_at.is_(None))
    )
    total = await db_session.scalar(
        select(func.count()).select_from(KbChunk).where(KbChunk.entry_id == result.entry_id)
    )
    assert pending == total > 0


async def test_capture_embeds_every_chunk_when_an_embedder_can(session_factory, db_session):
    embedder = FakeEmbedder()

    result = await capture_article(
        session_factory,
        embedder,
        url="https://example.test/xz",
        title="The xz backdoor",
        source_name="Example",
        text=ARTICLE,
        published_at=None,
        feed_item_id=None,
        captured_by="user",
    )

    pending = await db_session.scalar(
        select(func.count())
        .select_from(KbChunk)
        .where(KbChunk.entry_id == result.entry_id, KbChunk.embedded_at.is_(None))
    )
    vectors = await db_session.scalar(text("SELECT count(*) FROM kb_chunk_vec"))
    assert pending == 0
    assert vectors == len(embedder.documents) > 0


async def test_capture_never_holds_a_transaction_across_the_embedder_call(session_factory):
    """The embedder writes to the same database while it is being called.

    SQLite allows exactly one writer. If capture still held its write
    transaction open here this would block for ``busy_timeout`` and then raise,
    which is precisely the failure the rule exists to prevent.
    """

    class WritingEmbedder(FakeEmbedder):
        async def embed_documents(self, texts):
            async with session_factory() as session:
                session.add(KbActivity(action="embed", source="probe"))
                await session.commit()
            return await super().embed_documents(texts)

    result = await capture_article(
        session_factory,
        WritingEmbedder(),
        url="https://example.test/xz",
        title="The xz backdoor",
        source_name="Example",
        text=ARTICLE,
        published_at=None,
        feed_item_id=None,
        captured_by="user",
    )

    assert result.created is True


async def test_published_at_comes_from_the_feed_item_and_never_from_the_clock(
    session_factory, db_session
):
    published = datetime(2024, 3, 29, 12, 0, 0)

    result = await _capture(session_factory, published_at=published)

    entry = await db_session.get(KbEntry, result.entry_id)
    assert entry.published_at == published
    assert entry.captured_at != published


async def test_published_at_is_null_when_nothing_supplies_one(session_factory, db_session):
    result = await _capture(session_factory)

    entry = await db_session.get(KbEntry, result.entry_id)
    assert entry.published_at is None
    assert entry.captured_at is not None


async def test_capture_dedups_on_the_canonical_url_and_adds_a_back_link(
    session_factory, db_session
):
    first = await _capture(session_factory)
    item = await _feed_item(db_session)

    second = await _capture(
        session_factory,
        url="https://example.test/xz?utm_campaign=newsletter",
        feed_item_id=item.id,
    )

    assert second.entry_id == first.entry_id
    assert second.created is False
    entries = await db_session.scalar(select(func.count()).select_from(KbEntry))
    assert entries == 1
    links = (
        (
            await db_session.execute(
                select(KbEntryLink).where(KbEntryLink.entry_id == first.entry_id)
            )
        )
        .scalars()
        .all()
    )
    assert [link.feed_item_id for link in links] == [item.id]


async def test_capture_dedups_on_the_feed_item_id_when_the_url_moved(session_factory, db_session):
    item = await _feed_item(db_session)
    first = await _capture(session_factory, feed_item_id=item.id)

    second = await _capture(
        session_factory, url="https://example.test/xz-renamed", feed_item_id=item.id
    )

    assert second.entry_id == first.entry_id
    assert second.created is False
    assert await db_session.scalar(select(func.count()).select_from(KbEntry)) == 1


async def test_capture_dedups_on_the_content_hash_when_there_is_no_url(session_factory, db_session):
    first = await _capture(session_factory, url=None)

    second = await _capture(session_factory, url=None, title="The xz backdoor (mirror)")

    assert second.entry_id == first.entry_id
    assert second.created is False
    assert await db_session.scalar(select(func.count()).select_from(KbEntry)) == 1


async def test_a_note_whose_body_matches_an_article_still_gets_its_own_entry(
    session_factory, db_session
):
    """Spec §4.5: a note's one dedup key is its note id, never the content hash.

    The hash is the fallback for text with no key at all. Falling through to it
    handed the note the article's entry, and because that entry has no
    ``note_id`` the next save fell through again — the note could never acquire
    an entry of its own.
    """
    article = await _capture(session_factory)
    note = Note(title="Week 12", body_md=ARTICLE, template_used="t")
    db_session.add(note)
    await db_session.commit()

    captured = await capture_note(session_factory, NullEmbedder(), note.id)

    assert captured.created is True
    assert captured.entry_id != article.entry_id
    entry = await db_session.get(KbEntry, captured.entry_id)
    assert entry.note_id == note.id
    assert entry.kind == "note"
    assert await db_session.scalar(select(func.count()).select_from(KbEntry)) == 2


async def test_two_notes_with_the_same_body_are_two_entries(session_factory, db_session):
    first = Note(title="Week 12", body_md=ARTICLE, template_used="t")
    second = Note(title="Week 13", body_md=ARTICLE, template_used="t")
    db_session.add_all([first, second])
    await db_session.commit()

    one = await capture_note(session_factory, NullEmbedder(), first.id)
    two = await capture_note(session_factory, NullEmbedder(), second.id)

    assert two.created is True
    assert two.entry_id != one.entry_id
    note_ids = (
        (await db_session.execute(select(KbEntry.note_id).order_by(KbEntry.id))).scalars().all()
    )
    assert note_ids == [first.id, second.id]


async def test_a_short_snapshot_is_skipped_with_an_activity_row(session_factory, db_session):
    result = await _capture(session_factory, text="Too short to be an article.")

    assert result.entry_id is None
    assert result.created is False
    assert "minimum" in (result.skipped_reason or "")
    assert await db_session.scalar(select(func.count()).select_from(KbEntry)) == 0
    rows = (await db_session.execute(select(KbActivity))).scalars().all()
    assert [row.action for row in rows] == ["skip"]


async def test_the_minimum_length_is_the_documented_default(session_factory):
    assert DEFAULT_MIN_SNAPSHOT_CHARS == 400

    result = await _capture(session_factory, text="x" * (DEFAULT_MIN_SNAPSHOT_CHARS - 1))
    assert result.skipped_reason is not None

    result = await _capture(session_factory, text="y " * DEFAULT_MIN_SNAPSHOT_CHARS)
    assert result.created is True


async def test_capture_note_stores_the_body_as_a_human_authored_entry(session_factory, db_session):
    note = Note(title="Week 12", body_md=ARTICLE, template_used="t")
    db_session.add(note)
    await db_session.commit()

    result = await capture_note(session_factory, NullEmbedder(), note.id)

    entry = await db_session.get(KbEntry, result.entry_id)
    assert entry.kind == "note"
    assert entry.authorship == "human"
    assert entry.note_id == note.id
    assert entry.url is None
    assert entry.published_at is None


async def test_capture_note_twice_updates_the_snapshot_rather_than_duplicating(
    session_factory, db_session
):
    note = Note(title="Week 12", body_md=ARTICLE, template_used="t")
    db_session.add(note)
    await db_session.commit()
    first = await capture_note(session_factory, NullEmbedder(), note.id)

    note.body_md = ARTICLE + "\n\nAn addendum about CVE-2024-3094 mitigations everywhere.\n"
    await db_session.commit()
    second = await capture_note(session_factory, NullEmbedder(), note.id)

    assert second.entry_id == first.entry_id
    assert second.created is False
    versions = (
        (
            await db_session.execute(
                select(KbSnapshot.version).where(KbSnapshot.entry_id == first.entry_id)
            )
        )
        .scalars()
        .all()
    )
    assert sorted(versions) == [1, 2]
    hits = await _search(session_factory, "addendum")
    assert [hit.entry.id for hit in hits] == [first.entry_id]


async def test_capture_url_fetches_through_the_extractor(session_factory, db_session):
    transport = routes_transport(
        {"https://example.test/article": httpx2.Response(200, text=fixture_text("article.html"))}
    )

    result = await capture_url(
        session_factory, NullEmbedder(), "https://example.test/article", transport=transport
    )

    assert result.created is True
    entry = await db_session.get(KbEntry, result.entry_id)
    assert entry.url == "https://example.test/article"
    assert entry.captured_by == "user"


async def test_capture_url_reports_a_fetch_failure_instead_of_raising(session_factory, db_session):
    transport = routes_transport({"https://example.test/gone": httpx2.Response(500, text="no")})

    result = await capture_url(
        session_factory, NullEmbedder(), "https://example.test/gone", transport=transport
    )

    assert result.entry_id is None
    assert result.skipped_reason
    assert await db_session.scalar(select(func.count()).select_from(KbEntry)) == 0
    rows = (await db_session.execute(select(KbActivity))).scalars().all()
    assert [row.action for row in rows] == ["skip"]


# ------------------------------------------------------------------ refresh


async def test_refresh_with_identical_text_inserts_no_version(session_factory, db_session):
    transport = routes_transport(
        {"https://example.test/article": httpx2.Response(200, text=fixture_text("article.html"))}
    )
    captured = await capture_url(
        session_factory, NullEmbedder(), "https://example.test/article", transport=transport
    )

    refreshed = await refresh_snapshot(
        session_factory, NullEmbedder(), captured.entry_id, transport=transport
    )

    assert refreshed.changed is False
    assert refreshed.version == 1
    count = await db_session.scalar(
        select(func.count()).select_from(KbSnapshot).where(KbSnapshot.entry_id == captured.entry_id)
    )
    assert count == 1


async def test_refresh_with_changed_text_inserts_version_two_and_rechunks(
    session_factory, db_session
):
    original = fixture_text("article.html")
    updated = original.replace("</body>", "<p>" + ("A revised paragraph. " * 40) + "</p></body>")
    responses = [httpx2.Response(200, text=original), httpx2.Response(200, text=updated)]
    transport = routes_transport(
        {"https://example.test/article": lambda _request: responses.pop(0)}
    )
    captured = await capture_url(
        session_factory, NullEmbedder(), "https://example.test/article", transport=transport
    )

    refreshed = await refresh_snapshot(
        session_factory, NullEmbedder(), captured.entry_id, transport=transport
    )

    assert refreshed.changed is True
    assert refreshed.version == 2
    entry = await db_session.get(KbEntry, captured.entry_id)
    await db_session.refresh(entry)
    current = await db_session.get(KbSnapshot, entry.current_snapshot_id)
    assert current.version == 2
    # Search reads the current version only.
    hits = await _search(session_factory, "revised paragraph")
    assert [hit.entry.id for hit in hits] == [captured.entry_id]
    chunks = (
        (await db_session.execute(select(KbChunk).where(KbChunk.entry_id == captured.entry_id)))
        .scalars()
        .all()
    )
    assert {chunk.snapshot_id for chunk in chunks} == {current.id}


async def test_a_failed_refresh_keeps_the_text_it_was_going_to_replace(session_factory, db_session):
    original = fixture_text("article.html")
    responses = [httpx2.Response(200, text=original), httpx2.Response(503, text="down")]
    transport = routes_transport(
        {"https://example.test/article": lambda _request: responses.pop(0)}
    )
    captured = await capture_url(
        session_factory, NullEmbedder(), "https://example.test/article", transport=transport
    )

    refreshed = await refresh_snapshot(
        session_factory, NullEmbedder(), captured.entry_id, transport=transport
    )

    assert refreshed.changed is False
    assert refreshed.reason
    count = await db_session.scalar(
        select(func.count()).select_from(KbSnapshot).where(KbSnapshot.entry_id == captured.entry_id)
    )
    assert count == 1
    actions = (await db_session.execute(select(KbActivity.action))).scalars().all()
    assert "skip" in actions


async def test_refreshing_an_entry_with_no_url_is_a_reason_not_a_crash(session_factory):
    note_like = await _capture(session_factory, url=None)

    refreshed = await refresh_snapshot(session_factory, NullEmbedder(), note_like.entry_id)

    assert refreshed.changed is False
    assert "URL" in (refreshed.reason or "")


# -------------------------------------------------------------- soft delete


async def test_soft_delete_drops_the_chunks_and_hides_the_entry(session_factory, db_session):
    captured = await _capture(session_factory)
    assert await _search(session_factory, "liblzma")

    await soft_delete(session_factory, captured.entry_id)

    entry = await db_session.get(KbEntry, captured.entry_id)
    await db_session.refresh(entry)
    assert entry.deleted_at is not None
    assert await _search(session_factory, "liblzma") == []
    chunks = await db_session.scalar(
        select(func.count()).select_from(KbChunk).where(KbChunk.entry_id == captured.entry_id)
    )
    assert chunks == 0
    # The snapshot survives, because Undo re-chunks from it.
    snapshots = await db_session.scalar(
        select(func.count()).select_from(KbSnapshot).where(KbSnapshot.entry_id == captured.entry_id)
    )
    assert snapshots == 1


async def test_undelete_rechunks_from_the_current_snapshot(session_factory, db_session):
    captured = await _capture(session_factory)
    await soft_delete(session_factory, captured.entry_id)

    await undelete(session_factory, NullEmbedder(), captured.entry_id)

    entry = await db_session.get(KbEntry, captured.entry_id)
    await db_session.refresh(entry)
    assert entry.deleted_at is None
    assert [hit.entry.id for hit in await _search(session_factory, "liblzma")] == [
        captured.entry_id
    ]


async def test_undelete_refuses_when_the_url_has_been_recaptured(session_factory):
    captured = await _capture(session_factory)
    await soft_delete(session_factory, captured.entry_id)
    await _capture(session_factory)

    with pytest.raises(KbConflict):
        await undelete(session_factory, NullEmbedder(), captured.entry_id)


async def test_purge_removes_only_soft_deleted_entries(session_factory, db_session):
    deleted = await _capture(session_factory)
    alive = await _capture(session_factory, url="https://example.test/other")
    await soft_delete(session_factory, deleted.entry_id)

    with pytest.raises(KbConflict):
        await purge(session_factory, [deleted.entry_id, alive.entry_id])

    assert await purge(session_factory, [deleted.entry_id]) == 1
    assert await db_session.get(KbEntry, deleted.entry_id) is None
    assert await db_session.get(KbEntry, alive.entry_id) is not None


# -------------------------------------------------------------------- merge


async def test_merge_keeps_the_older_entry_and_unions_everything(session_factory, db_session):
    older = await _capture(session_factory, url="https://example.test/older")
    newer = await _capture(session_factory, url="https://example.test/newer", text=ARTICLE + "!")
    item = await _feed_item(db_session)
    topic = Topic(name="supply chain")
    db_session.add(topic)
    await db_session.flush()
    db_session.add_all(
        [
            KbEntryTopic(entry_id=newer.entry_id, topic_id=topic.id, suggested=True),
            KbEntryTag(entry_id=newer.entry_id, tag="xz"),
            KbEntryLink(entry_id=newer.entry_id, feed_item_id=item.id),
            KbEntryEntity(entry_id=newer.entry_id, kind="vendor", value="Red Hat", source="user"),
        ]
    )
    older_entry = await db_session.get(KbEntry, older.entry_id)
    newer_entry = await db_session.get(KbEntry, newer.entry_id)
    older_entry.notes_md = "mine"
    newer_entry.notes_md = "theirs"
    # Make the age difference explicit rather than relying on clock resolution.
    older_entry.captured_at = utcnow() - timedelta(days=2)
    await db_session.commit()

    kept = await merge_entries(session_factory, newer.entry_id, older.entry_id)

    assert kept == older.entry_id
    await db_session.refresh(older_entry)
    await db_session.refresh(newer_entry)
    assert newer_entry.deleted_at is not None
    assert "mine" in older_entry.notes_md and "theirs" in older_entry.notes_md
    topics = (
        (await db_session.execute(select(KbEntryTopic).where(KbEntryTopic.entry_id == kept)))
        .scalars()
        .all()
    )
    assert [row.topic_id for row in topics] == [topic.id]
    tags = (
        (await db_session.execute(select(KbEntryTag.tag).where(KbEntryTag.entry_id == kept)))
        .scalars()
        .all()
    )
    assert tags == ["xz"]
    links = (
        (
            await db_session.execute(
                select(KbEntryLink.feed_item_id).where(KbEntryLink.entry_id == kept)
            )
        )
        .scalars()
        .all()
    )
    assert links == [item.id]
    entities = (
        (
            await db_session.execute(
                select(KbEntryEntity.value).where(
                    KbEntryEntity.entry_id == kept, KbEntryEntity.kind == "vendor"
                )
            )
        )
        .scalars()
        .all()
    )
    assert entities == ["Red Hat"]


async def test_merge_keeps_the_older_entry_whichever_way_round_it_is_asked(session_factory):
    older = await _capture(session_factory, url="https://example.test/older")
    newer = await _capture(session_factory, url="https://example.test/newer", text=ARTICLE + "!")

    kept = await merge_entries(session_factory, older.entry_id, newer.entry_id)

    assert kept == older.entry_id


# ------------------------------------------------------------- activity log


async def test_the_activity_log_is_pruned_above_its_ceiling(
    session_factory, db_session, monkeypatch
):
    monkeypatch.setattr(capture_module, "ACTIVITY_MAX_ROWS", 5)
    async with session_factory() as session:
        session.add_all([KbActivity(action="capture", source="seed") for _ in range(20)])
        await session.commit()

    await _capture(session_factory)

    remaining = await db_session.scalar(select(func.count()).select_from(KbActivity))
    assert remaining == 5


# --------------------------------------------------------------- acceptance


async def test_a_long_article_is_findable_by_a_phrase_in_its_last_paragraph(session_factory):
    filler = "\n\n".join(
        f"Paragraph {n} about routine patch management and vendor advisories." for n in range(600)
    )
    tail = "The final paragraph names the quarantined telemetry beacon."
    body = f"# Long advisory\n\n{filler}\n\n{tail}"
    assert len(body) > 40_000

    captured = await _capture(session_factory, text=body, url="https://example.test/long")

    hits = await _search(session_factory, "quarantined telemetry beacon")
    assert [hit.entry.id for hit in hits] == [captured.entry_id]


# ------------------------------------------------- regressions, fix round 1


def test_canonical_url_keeps_a_valueless_parameter_and_its_percent_encoding():
    """The query is filtered, never re-encoded.

    Round-tripping it through ``parse_qsl``/``urlencode`` turned ``?b`` into
    ``?b=`` and ``%20`` into ``+`` — both are different requests to plenty of
    servers, and the stored URL is what "Refresh snapshot" re-fetches.
    """
    assert canonical_url("https://example.test/a?b&x=a%20b") == "https://example.test/a?b&x=a%20b"


def test_canonical_url_strips_one_trailing_slash_and_never_the_roots():
    assert canonical_url("https://example.test/a/") == "https://example.test/a"
    # Only one: `/a//` and `/a/` are different resources, and so are `/a/` and `/a`.
    assert canonical_url("https://example.test/a//") == "https://example.test/a/"
    assert canonical_url("https://example.test/") == "https://example.test/"
    assert canonical_url("https://example.test") == "https://example.test/"


def test_canonical_url_lowercases_the_host_and_drops_only_the_default_port():
    assert canonical_url("https://EXAMPLE.test:443/A") == "https://example.test/A"
    assert canonical_url("https://example.test:8443/a") == "https://example.test:8443/a"
    assert canonical_url("http://example.test:80/a") == "http://example.test/a"


def test_canonical_url_removes_tracking_parameters_and_keeps_the_rest_in_order():
    assert (
        canonical_url("https://example.test/a?utm_source=x&id=7&fbclid=y&page=2")
        == "https://example.test/a?id=7&page=2"
    )


async def test_editing_a_note_whose_entry_is_deleted_leaves_it_deleted(session_factory, db_session):
    """A deleted entry has no chunks, and an edit must not give it some.

    It could not be embedded either — ``embed_pending`` skips deleted entries —
    so the "N chunks not embedded" badge would have counted a chunk that nothing
    could ever clear.
    """
    note = Note(title="Week 12", body_md=ARTICLE, template_used="t")
    db_session.add(note)
    await db_session.commit()
    captured = await capture_note(session_factory, NullEmbedder(), note.id)
    await soft_delete(session_factory, captured.entry_id)

    note.body_md = ARTICLE + "\n\nAn addendum about CVE-2024-3094 mitigations everywhere.\n"
    await db_session.commit()
    await capture_note(session_factory, NullEmbedder(), note.id)

    entry = await db_session.get(KbEntry, captured.entry_id)
    await db_session.refresh(entry)
    assert entry.deleted_at is not None
    chunks = await db_session.scalar(
        select(func.count()).select_from(KbChunk).where(KbChunk.entry_id == captured.entry_id)
    )
    assert chunks == 0

    # The new text was still kept, and Undo is what brings it back.
    await undelete(session_factory, NullEmbedder(), captured.entry_id)
    hits = await _search(session_factory, "addendum")
    assert [hit.entry.id for hit in hits] == [captured.entry_id]


class _ExpiringSession(AsyncSession):
    """A session that expires everything on the way out.

    Exactly what one added ``commit()`` inside a read block, or one
    ``expire_on_commit=True`` on the factory, would do to an instance the caller
    kept a reference to. ``close()`` expunging without expiring is a property of
    today's settings, not a promise.
    """

    async def close(self) -> None:
        self.expire_all()
        await super().close()


async def test_a_capture_reads_nothing_off_a_closed_session(db_engine, db_session):
    """Every value a capture needs is copied out before its session closes."""
    expiring = async_sessionmaker(db_engine, class_=_ExpiringSession, expire_on_commit=False)
    feed = Feed(url="https://example.test/feed.xml", title="Example Feed")
    db_session.add(feed)
    await db_session.flush()
    item = FeedItem(
        feed_id=feed.id,
        guid="xz-1",
        title="The xz backdoor",
        url="https://example.test/xz",
        content_text=ARTICLE,
        published_at=datetime(2024, 3, 29, 12, 0, 0),
    )
    # A body of its own, so the two captures are visibly two entries rather than
    # one read twice.
    note = Note(title="Week 12", body_md=ARTICLE.replace("xz", "polkit"), template_used="t")
    db_session.add_all([item, note])
    await db_session.commit()
    service = KbService(session_factory=expiring, embedder=NullEmbedder())

    from_item = await service.capture_feed_item(item.id, captured_by="user")
    from_note = await capture_note(expiring, NullEmbedder(), note.id)

    assert from_item.created is True
    assert from_note.created is True
    entry = await db_session.get(KbEntry, from_item.entry_id)
    assert entry.url == "https://example.test/xz"
    assert entry.published_at == datetime(2024, 3, 29, 12, 0, 0)


async def test_an_embedder_failure_after_the_commit_is_an_activity_row(session_factory, db_session):
    """Spec §5: capture completes and the chunks stay pending.

    The embed call happens after the commit, so letting it raise would 500 a
    request whose entry is already in the database.
    """

    class BrokenEmbedder(FakeEmbedder):
        async def embed_documents(self, texts):
            raise RuntimeError("Voyage is down")

    result = await capture_article(
        session_factory,
        BrokenEmbedder(),
        url="https://example.test/xz",
        title="The xz backdoor",
        text=ARTICLE,
        captured_by="user",
    )

    assert result.created is True
    assert await db_session.get(KbEntry, result.entry_id) is not None
    pending = await db_session.scalar(
        select(func.count())
        .select_from(KbChunk)
        .where(KbChunk.entry_id == result.entry_id, KbChunk.embedded_at.is_(None))
    )
    assert pending > 0
    rows = (await db_session.execute(select(KbActivity))).scalars().all()
    assert [row.action for row in rows] == ["capture", "skip"]
    assert "Voyage is down" in rows[1].detail


# ------------------------------------------------- a pasted URL, round 1


PLAIN_PAGE = (
    "<!doctype html><html><body><article>"
    + "".join(
        f"<p>Paragraph {n} about the liblzma backdoor and the mitigation it needs, "
        "repeated at length so the extractor has something to work with.</p>"
        for n in range(12)
    )
    + "</article></body></html>"
)


async def test_capture_url_takes_its_title_from_the_page(session_factory, db_session):
    """A hand-saved URL showed its own URL as the title, in every list that has one."""
    transport = routes_transport(
        {"https://example.test/article": httpx2.Response(200, text=fixture_text("article.html"))}
    )

    result = await capture_url(
        session_factory, NullEmbedder(), "https://example.test/article", transport=transport
    )

    entry = await db_session.get(KbEntry, result.entry_id)
    assert entry.title == "Critical RCE patched in ExampleOS"


async def test_capture_url_keeps_a_title_the_user_typed(session_factory, db_session):
    transport = routes_transport(
        {"https://example.test/article": httpx2.Response(200, text=fixture_text("article.html"))}
    )

    result = await capture_url(
        session_factory,
        NullEmbedder(),
        "https://example.test/article",
        title="What I want to call it",
        transport=transport,
    )

    entry = await db_session.get(KbEntry, result.entry_id)
    assert entry.title == "What I want to call it"


async def test_capture_url_falls_back_to_the_url_when_the_page_names_itself_nothing(
    session_factory, db_session
):
    """The URL is the last resort, not the first: it is a label nobody can scan."""
    transport = routes_transport(
        {"https://example.test/untitled": httpx2.Response(200, text=PLAIN_PAGE)}
    )

    result = await capture_url(
        session_factory, NullEmbedder(), "https://example.test/untitled", transport=transport
    )

    entry = await db_session.get(KbEntry, result.entry_id)
    assert entry.title == "https://example.test/untitled"


async def test_a_pasted_url_is_a_manual_entry(session_factory, db_session):
    """`manual` is the spec's kind for an explicit save; `article` is what a feed gives.

    The Knowledge page filters on it, so conflating the two made "saved by hand"
    match nothing.
    """
    transport = routes_transport(
        {"https://example.test/article": httpx2.Response(200, text=fixture_text("article.html"))}
    )

    result = await capture_url(
        session_factory, NullEmbedder(), "https://example.test/article", transport=transport
    )

    entry = await db_session.get(KbEntry, result.entry_id)
    assert entry.kind == "manual"


# ------------------------------------------------------------ near-duplicates


class MarkerEmbedder:
    """Unit vectors whose pairwise cosine is decided by a ``@@marker@@`` in the text.

    Two texts carrying the same marker embed to the *same* one-hot unit vector
    (cosine 1.0); two carrying different markers embed to orthogonal ones (cosine
    0.0). ``FakeEmbedder`` cannot do this — it hashes the whole text, so a
    near-copy is as far away as an unrelated article — and "close vector" has to
    be exact for a threshold test to mean anything.
    """

    model = "marker-embed-1"
    dimensions = VEC_DIMENSIONS

    def __init__(self) -> None:
        self.calls = 0

    async def embed_documents(self, texts):
        self.calls += 1
        return [self._vector(text) for text in texts]

    async def embed_query(self, text):
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> list[float]:
        found = re.search(r"@@(\w+)@@", text)
        key = found.group(1) if found else text
        axis = int(hashlib.sha256(key.encode()).hexdigest(), 16) % VEC_DIMENSIONS
        vector = [0.0] * VEC_DIMENSIONS
        vector[axis] = 1.0
        return vector


#: Two spellings of one headline, as two feeds would carry it.
HEADLINE = "Backdoor found in xz utils, tracked as CVE-2024-3094"
HEADLINE_RETITLED = "Backdoor found in xz-utils, tracked as CVE-2024-3094"


def _marked(marker: str) -> str:
    """The standard article body, tagged so :class:`MarkerEmbedder` can place it."""
    return f"@@{marker}@@ {ARTICLE}"


def test_title_similarity_is_one_for_identical_titles_and_low_for_unrelated_ones():
    assert capture_module.title_similarity("The xz backdoor", "the  XZ   Backdoor") == 1.0
    assert capture_module.title_similarity("The xz backdoor", "Quarterly revenue report") < 0.2
    # The case the whole rule exists for: the same headline, punctuated differently
    # by two feeds. 0.8 is strict — a title that gained a clause does not clear it.
    assert (
        capture_module.title_similarity(HEADLINE, HEADLINE_RETITLED)
        >= capture_module.TITLE_TRIGRAM_MIN
    )
    assert capture_module.title_similarity("The xz backdoor", "The xz backdoor, explained") < (
        capture_module.TITLE_TRIGRAM_MIN
    )
    assert capture_module.title_similarity("", "anything") == 0.0


def test_cosine_from_distance_is_not_inverted():
    """vec0 defaults to **L2**, so the conversion is ``1 - d²/2``, not ``1 - d``.

    Pinned with the three distances an L2 KNN over unit vectors actually returns:
    0 for the same vector, sqrt(2) for orthogonal ones, 2 for opposite ones.
    """
    assert capture_module.cosine_from_distance(0.0) == pytest.approx(1.0)
    assert capture_module.cosine_from_distance(math.sqrt(2)) == pytest.approx(0.0, abs=1e-9)
    assert capture_module.cosine_from_distance(2.0) == pytest.approx(-1.0)
    # The inverted reading would call this a duplicate; the right one does not.
    assert capture_module.cosine_from_distance(0.5) == pytest.approx(0.875)


def test_a_high_cosine_with_a_dissimilar_title_does_not_flag():
    """Both legs must hold. Syndicated text under two different headlines is two
    articles, and merging them is the user's call, not a threshold's."""
    assert capture_module.is_near_duplicate(0.99, 0.95, threshold=0.92) is True
    assert capture_module.is_near_duplicate(0.99, 0.30, threshold=0.92) is False
    assert capture_module.is_near_duplicate(0.50, 0.95, threshold=0.92) is False
    # Exactly on both thresholds still counts.
    assert capture_module.is_near_duplicate(0.92, 0.8, threshold=0.92) is True
    # No embedder: the trigram runs alone.
    assert capture_module.is_near_duplicate(None, 0.95, threshold=0.92) is True
    assert capture_module.is_near_duplicate(None, 0.30, threshold=0.92) is False


async def test_capture_flags_a_near_duplicate_from_the_first_body_chunk(
    session_factory, db_session
):
    """The check runs at capture time, off the vector the embed just wrote — long
    before any compile, which is the only thing that could produce a summary chunk."""
    embedder = MarkerEmbedder()
    first = await capture_article(
        session_factory,
        embedder,
        url="https://example.test/xz-one",
        title=HEADLINE,
        text=_marked("xz"),
    )
    second = await capture_article(
        session_factory,
        embedder,
        url="https://example.test/xz-two",
        title=HEADLINE_RETITLED,
        text=_marked("xz"),
    )

    assert second.possible_duplicate_of == first.entry_id
    older = await db_session.get(KbEntry, first.entry_id)
    newer = await db_session.get(KbEntry, second.entry_id)
    # The flag points backwards and the older entry is untouched.
    assert older.possible_duplicate_of is None
    assert newer.possible_duplicate_of == older.id
    # Nothing compiled: the check reads the body, never a summary.
    assert older.compiled_at is None and newer.compiled_at is None
    assert older.summary_md is None and newer.summary_md is None


async def test_a_close_vector_under_an_unrelated_title_is_not_flagged(session_factory):
    """The end-to-end half of the pure test above: same text, different headline."""
    embedder = MarkerEmbedder()
    await capture_article(
        session_factory,
        embedder,
        url="https://example.test/xz-one",
        title="The xz backdoor",
        text=_marked("xz"),
    )
    second = await capture_article(
        session_factory,
        embedder,
        url="https://example.test/xz-two",
        title="Quarterly revenue guidance for the fiscal year",
        text=_marked("xz"),
    )

    assert second.possible_duplicate_of is None


async def test_an_identical_title_over_different_text_is_not_flagged(session_factory):
    """The other leg on its own: the same headline, a different story."""
    embedder = MarkerEmbedder()
    await capture_article(
        session_factory,
        embedder,
        url="https://example.test/one",
        title="The xz backdoor",
        text=_marked("xz"),
    )
    second = await capture_article(
        session_factory,
        embedder,
        url="https://example.test/two",
        title="The xz backdoor",
        text=_marked("unrelated"),
    )

    assert second.possible_duplicate_of is None


async def test_with_no_embedder_only_the_trigram_runs_and_it_only_flags(
    session_factory, db_session
):
    """``NullEmbedder`` leaves every chunk pending, so there is no vector to
    compare — the title test runs alone, and a hit is still only ever a flag."""
    first = await _capture(session_factory, url="https://example.test/one", title=HEADLINE)
    second = await _capture(
        session_factory, url="https://example.test/two", title=HEADLINE_RETITLED
    )

    assert second.possible_duplicate_of == first.entry_id
    # Flag, never merge: two entries, both alive.
    live = (
        await db_session.execute(select(KbEntry).where(KbEntry.deleted_at.is_(None)))
    ).scalars().all()
    assert len(live) == 2
    assert {row.id for row in live} == {first.entry_id, second.entry_id}


async def test_the_flag_is_a_column_and_the_trail_names_both_scores(session_factory, db_session):
    """``possible_duplicate_of`` is a column, never a string inside a detail (spec §8)
    — the detail carries the *evidence*, which is what makes 0.92 calibratable."""
    first = await _capture(session_factory, url="https://example.test/one")
    second = await _capture(session_factory, url="https://example.test/two")

    rows = (
        await db_session.execute(
            select(KbActivity).where(KbActivity.entry_id == second.entry_id)
        )
    ).scalars().all()
    detail = next(row.detail for row in rows if "possible duplicate" in (row.detail or ""))
    assert f"entry {first.entry_id}" in detail
    assert "cosine n/a" in detail and "title 1.00" in detail


async def test_deferring_the_embedding_skips_the_check_and_leaves_the_chunks_pending(
    session_factory, db_session
):
    """What the bulk job asks for: no Voyage call, no vector, and no flag from a
    rule that would have had to guess without one."""
    embedder = MarkerEmbedder()
    await capture_article(
        session_factory,
        embedder,
        url="https://example.test/one",
        title="The xz backdoor",
        text=_marked("xz"),
    )
    second = await capture_article(
        session_factory,
        embedder,
        url="https://example.test/two",
        title="The xz backdoor",
        text=_marked("xz"),
        defer_embedding=True,
    )

    assert second.possible_duplicate_of is None
    pending = await db_session.scalar(
        select(func.count())
        .select_from(KbChunk)
        .where(KbChunk.entry_id == second.entry_id, KbChunk.embedded_at.is_(None))
    )
    assert pending > 0


async def test_a_failed_embed_leaves_the_entry_unflagged_rather_than_title_matched(
    session_factory,
):
    """An embedder that is configured but broken must not silently downgrade the
    rule to the title alone — that is the no-embedder case, not this one."""

    class BrokenEmbedder(MarkerEmbedder):
        async def embed_documents(self, texts):
            raise EmbeddingError(429, "rate limited")

    await capture_article(
        session_factory,
        MarkerEmbedder(),
        url="https://example.test/one",
        title="The xz backdoor",
        text=_marked("xz"),
    )
    second = await capture_article(
        session_factory,
        BrokenEmbedder(),
        url="https://example.test/two",
        title="The xz backdoor",
        text=_marked("xz"),
    )

    assert second.possible_duplicate_of is None


class AngledEmbedder:
    """Two unit vectors a **known** cosine apart: 0.8 for a ``@@near@@`` text.

    ``MarkerEmbedder`` only produces 1.0 and 0.0, which no threshold between them
    can tell apart. A pair sitting at exactly 0.8 is what makes the setting the
    only thing deciding the outcome.
    """

    model = "angled-embed-1"
    dimensions = VEC_DIMENSIONS

    async def embed_documents(self, texts):
        return [self._vector(text) for text in texts]

    async def embed_query(self, text):
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> list[float]:
        vector = [0.0] * VEC_DIMENSIONS
        if "@@near@@" in text:
            vector[0], vector[1] = 0.8, 0.6
        else:
            vector[0] = 1.0
        return vector


async def test_the_article_fetch_holds_no_database_connection(
    session_factory, db_session, db_engine
):
    """Nothing is checked out of the pool while a capture waits on a website.

    One at a time this was merely untidy; ``MAX_KB_EXTRACTIONS = 8`` made it eight
    pooled connections parked on network I/O for up to ``feed_timeout_s`` each,
    with every other request in the application competing for what is left.
    """
    feed = Feed(url="https://example.test/feed.xml", title="Example")
    db_session.add(feed)
    await db_session.flush()
    item = FeedItem(
        feed_id=feed.id,
        guid="needs-extraction",
        title="An advisory with no stored text",
        url="https://example.test/article",
    )
    db_session.add(item)
    await db_session.commit()

    checked_out: list[int] = []
    page = fixture_text("article.html")

    def handle(request: httpx2.Request) -> httpx2.Response:
        checked_out.append(db_engine.pool.checkedout())
        return httpx2.Response(200, text=page)

    service = KbService(session_factory=session_factory, transport=httpx2.MockTransport(handle))
    baseline = db_engine.pool.checkedout()

    result = await service.capture_feed_item(item.id)

    assert result.created is True
    assert checked_out == [baseline]


async def test_an_item_deleted_while_its_article_is_fetched_is_a_lookup_error(
    session_factory, db_session
):
    """The fetch happens between two short transactions, and the row can go in between.

    ``extract_item`` used to raise ``LookupError`` for this; fetching outside the
    session must not turn it into an ``AttributeError`` on ``None`` — a 500 on
    ``POST /api/kb/entries`` and an unreadable ``skip`` row on the star path.
    """
    feed = Feed(url="https://example.test/feed.xml", title="Example")
    db_session.add(feed)
    await db_session.flush()
    item = FeedItem(
        feed_id=feed.id,
        guid="vanishes",
        title="An advisory that is deleted mid-fetch",
        url="https://example.test/article",
    )
    db_session.add(item)
    await db_session.commit()
    item_id = item.id
    page = fixture_text("article.html")

    async def handle(request: httpx2.Request) -> httpx2.Response:
        async with session_factory() as session:
            await session.delete(await session.get(FeedItem, item_id))
            await session.commit()
        return httpx2.Response(200, text=page)

    service = KbService(session_factory=session_factory, transport=httpx2.MockTransport(handle))

    with pytest.raises(LookupError):
        await service.capture_feed_item(item_id)


async def test_a_long_entrys_own_chunks_do_not_crowd_the_duplicate_out_of_the_knn(
    session_factory, db_session
):
    """A fourteen-chunk article still finds the copy that was already there.

    Every one of the new entry's own body chunks is a candidate the KNN returns
    and the ``id < entry_id`` rule then throws away, so asking for
    ``NEAR_DUPLICATE_K + 1`` neighbours and filtering afterwards means a long
    advisory fills all six slots with itself and the syndicated re-publication is
    never seen. The exclusion belongs inside the KNN.
    """
    older = await capture_article(
        session_factory,
        AngledEmbedder(),
        url="https://example.test/advisory-first",
        title=HEADLINE,
        text=f"@@near@@ {ARTICLE}",
        duplicate_threshold=0.7,
    )
    long_body = "\n\n".join(f"## Section {index}\n\n{ARTICLE}" for index in range(60))
    newer = await capture_article(
        session_factory,
        AngledEmbedder(),
        url="https://example.test/advisory-syndicated",
        title=HEADLINE_RETITLED,
        text=long_body,
        duplicate_threshold=0.7,
    )

    chunks = await db_session.scalar(
        select(func.count()).select_from(KbChunk).where(KbChunk.entry_id == newer.entry_id)
    )
    assert chunks >= 14, "the scenario needs an entry with more chunks than the KNN asks for"
    assert newer.possible_duplicate_of == older.entry_id


@pytest.mark.parametrize(("threshold", "flagged"), [("0.92", False), ("0.70", True)])
async def test_the_duplicate_threshold_setting_decides_what_a_single_capture_flags(
    session_factory, db_session, threshold, flagged
):
    """A star, a pasted URL or a note reads ``kb_duplicate_threshold`` too.

    Only the bulk route used to: every other path took ``capture_article``'s 0.92
    default, so the setting in Settings → Knowledge moved nothing that was not
    captured two hundred at a time.
    """
    async with session_factory() as session:
        await settings_service.set_many(session, {"kb_duplicate_threshold": threshold})
        await session.commit()
    feed = Feed(url="https://example.test/feed.xml", title="Example")
    db_session.add(feed)
    await db_session.flush()
    first = FeedItem(
        feed_id=feed.id,
        guid="xz-1",
        title=HEADLINE,
        url="https://example.test/one",
        content_text=ARTICLE,
    )
    second = FeedItem(
        feed_id=feed.id,
        guid="xz-2",
        title=HEADLINE_RETITLED,
        url="https://example.test/two",
        content_text=f"@@near@@ {ARTICLE}",
    )
    db_session.add_all([first, second])
    await db_session.commit()
    service = KbService(session_factory=session_factory, embedder=AngledEmbedder())

    older = await service.capture_feed_item(first.id)
    newer = await service.capture_feed_item(second.id)

    assert (newer.possible_duplicate_of == older.entry_id) is flagged
