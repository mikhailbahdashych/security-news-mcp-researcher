"""The bulk capture job: ``POST /api/kb/bulk``, its frames, its cap and its cancel.

Every article is fetched through an ``httpx2.MockTransport`` handed to the service
by the same dependency override the production routes use, so nothing here
reaches the network. The tests about *timing* — the two concurrency caps and the
cancel — are driven by :class:`asyncio.Event`, a queue and a semaphore, never by
a sleep: a cap asserted after 80 milliseconds is a cap that passes on a fast
machine and reports nothing. None of them waits for something *not* to happen
either; every step waits for an arrival a broken implementation would also make,
which is what lets them fail rather than hang.
"""

from __future__ import annotations

import asyncio

import httpx2
import pytest
from fakes.embedder import FakeEmbedder
from feed_fixtures import fixture_text, routes_transport
from httpx2 import ASGITransport
from sqlalchemy import func, select
from sse_util import event_names, parse_sse, payloads_for

from app.api import tasks as task_registry
from app.api.deps import get_kb_service
from app.api.streaming import ALREADY_RUNNING_MESSAGE, CANCELLED_MESSAGE
from app.db.models import Feed, FeedItem
from app.kb.bulk import MAX_KB_EXTRACTIONS, bulk_key
from app.kb.capture import capture_article
from app.kb.embeddings import NullEmbedder
from app.kb.models import KbActivity, KbChunk, KbEntry
from app.kb.service import KbService
from app.services import settings as settings_service
from tests.fakes.anthropic import ScriptedAnthropic

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


@pytest.fixture(autouse=True)
async def clean_task_registry():
    await task_registry.clear()
    yield
    await task_registry.clear()


@pytest.fixture
async def feed(db_session) -> Feed:
    row = Feed(url="https://example.test/feed.xml", title="Example Feed")
    db_session.add(row)
    await db_session.commit()
    return row


async def seed_items(db_session, feed: Feed, count: int, *, stored_text: bool = True) -> list[int]:
    """*count* feed items, each with its own URL and title.

    With ``stored_text`` the snapshot is already in the row and no fetch happens;
    without it the job has to extract, which is what exercises the concurrency cap
    and the URL guard.
    """
    ids = []
    for index in range(count):
        item = FeedItem(
            feed_id=feed.id,
            guid=f"item-{index}",
            title=f"Article number {index}: a security advisory worth keeping",
            url=f"https://example.test/article-{index}",
            summary="short",
            content_text=BODY if stored_text else None,
        )
        db_session.add(item)
        await db_session.flush()
        ids.append(item.id)
    await db_session.commit()
    return ids


def article_transport(count: int):
    """``article.html`` at every seeded item's URL."""
    page = fixture_text("article.html")
    return routes_transport(
        {
            f"https://example.test/article-{index}": httpx2.Response(200, text=page)
            for index in range(count)
        }
    )


@pytest.fixture
def kb(app, session_factory):
    """Wire the app's knowledge base to the test database, keyword-only."""
    service = KbService(session_factory=session_factory, transport=article_transport(64))
    app.dependency_overrides[get_kb_service] = lambda: service
    return service


def progress(body: str) -> list[dict]:
    """The per-item payloads, unwrapped from the ``text_delta`` frames they ride in."""
    import json

    return [json.loads(payload["text"]) for payload in payloads_for(body, "text_delta")]


# ------------------------------------------------------------------- frames


async def test_the_bulk_job_emits_one_progress_frame_per_item_and_a_terminal_done(
    client, kb, db_session, feed
):
    ids = await seed_items(db_session, feed, 3)

    response = await client.post("/api/kb/bulk", json={"item_ids": ids, "job_id": "j1"})

    assert response.status_code == 200
    names = event_names(response.text)
    assert names == ["turn_start", "text_delta", "text_delta", "text_delta", "done"]

    started = parse_sse(response.text)[0][1]
    assert started["turn"] == 0
    # The client needs an id to press Stop, and may not have sent one.
    assert started["job_id"] == "j1"

    items = progress(response.text)
    assert {row["item_id"] for row in items} == set(ids)
    assert [row["done"] for row in items] == [1, 2, 3]
    assert {row["total"] for row in items} == {3}
    assert all(row["created"] for row in items)
    assert all(row["skipped_reason"] is None for row in items)

    done = payloads_for(response.text, "done")[0]
    assert done["saved"] == 3
    assert done["skipped"] == 0
    assert sorted(done["entry_ids"]) == sorted(row["entry_id"] for row in items)

    stored = await db_session.scalar(select(func.count()).select_from(KbEntry))
    assert stored == 3


async def test_an_item_that_cannot_be_extracted_is_reported_and_does_not_kill_the_job(
    app, client, session_factory, db_session, feed
):
    """One 404 is one skipped item and a row in the trail, not a failed run."""
    ids = await seed_items(db_session, feed, 3, stored_text=False)
    page = fixture_text("article.html")
    transport = routes_transport(
        {
            "https://example.test/article-0": httpx2.Response(200, text=page),
            # article-1 is not routed at all: a 404 from the mock transport.
            "https://example.test/article-2": httpx2.Response(200, text=page),
        }
    )
    app.dependency_overrides[get_kb_service] = lambda: KbService(
        session_factory=session_factory, transport=transport
    )

    response = await client.post("/api/kb/bulk", json={"item_ids": ids, "job_id": "j-404"})

    items = {row["item_id"]: row for row in progress(response.text)}
    assert len(items) == 3
    assert items[ids[1]]["entry_id"] is None
    assert items[ids[1]]["skipped_reason"]
    assert items[ids[0]]["entry_id"] and items[ids[2]]["entry_id"]

    done = payloads_for(response.text, "done")[0]
    assert done["saved"] == 2
    assert done["skipped"] == 1


async def test_a_second_job_for_the_same_key_gets_the_registry_error_frame(
    client, kb, db_session, feed, monkeypatch
):
    """The 409 covers the fast path; this covers the race it cannot close."""
    ids = await seed_items(db_session, feed, 1)
    holder = asyncio.create_task(asyncio.Event().wait())
    await task_registry.register(bulk_key("race"), holder)

    async def never_running(_key: str) -> bool:
        return False

    monkeypatch.setattr(task_registry, "is_running", never_running)
    try:
        response = await client.post(
            "/api/kb/bulk", json={"item_ids": ids, "job_id": "race"}
        )
    finally:
        holder.cancel()

    assert event_names(response.text) == ["error"]
    error = payloads_for(response.text, "error")[0]
    assert error["type"] == "api_error"
    assert error["message"] == ALREADY_RUNNING_MESSAGE
    # Nothing ran, so there is nothing to claim was saved.
    assert await db_session.scalar(select(func.count()).select_from(KbEntry)) == 0


async def test_a_second_post_while_a_job_runs_is_a_conflict(client, kb, db_session, feed):
    ids = await seed_items(db_session, feed, 1)
    holder = asyncio.create_task(asyncio.Event().wait())
    await task_registry.register(bulk_key("busy"), holder)
    try:
        response = await client.post("/api/kb/bulk", json={"item_ids": ids, "job_id": "busy"})
    finally:
        holder.cancel()

    assert response.status_code == 409
    assert "already running" in response.json()["detail"]


# ------------------------------------------------------- embedding, once


async def test_the_run_embeds_once_rather_than_once_per_entry(
    app, client, session_factory, db_session, feed
):
    """Two hundred articles must not be two hundred Voyage calls and two hundred
    rows in the trail. The run defers every embed and does one at the end."""
    ids = await seed_items(db_session, feed, 4)
    embedder = FakeEmbedder()
    app.dependency_overrides[get_kb_service] = lambda: KbService(
        session_factory=session_factory, embedder=embedder, transport=article_transport(8)
    )

    response = await client.post("/api/kb/bulk", json={"item_ids": ids, "job_id": "j-embed"})

    assert payloads_for(response.text, "done")[0]["saved"] == 4
    assert embedder.calls == 1
    rows = (
        (await db_session.execute(select(KbActivity).where(KbActivity.action == "embed")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    # And nothing is left pending: the deferral is a deferral, not a skip.
    pending = await db_session.scalar(
        select(func.count()).select_from(KbChunk).where(KbChunk.embedded_at.is_(None))
    )
    assert pending == 0


async def test_the_end_of_run_embed_never_touches_a_backlog_the_run_did_not_create(
    app, client, session_factory, db_session, feed
):
    """Saving three items must not send a nine-thousand-chunk backlog to Voyage.

    Pending chunks are the knowledge base's resting state — everything captured
    before a Voyage key existed is pending — and **Embed now** is the button that
    clears them, on purpose. A three-item save that silently embedded the lot
    would sit silent for minutes with no progress and no Stop, and would bill the
    whole backlog to one ``bulk`` row.
    """
    backlog = await capture_article(
        session_factory,
        NullEmbedder(),
        url="https://example.test/backlog",
        title="An advisory captured before the key was configured",
        text=BODY,
    )
    ids = await seed_items(db_session, feed, 3)
    app.dependency_overrides[get_kb_service] = lambda: KbService(
        session_factory=session_factory, embedder=FakeEmbedder(), transport=article_transport(8)
    )

    response = await client.post("/api/kb/bulk", json={"item_ids": ids, "job_id": "j-scope"})

    assert payloads_for(response.text, "done")[0]["saved"] == 3
    still_pending = await db_session.scalar(
        select(func.count())
        .select_from(KbChunk)
        .where(KbChunk.entry_id == backlog.entry_id, KbChunk.embedded_at.is_(None))
    )
    assert still_pending > 0
    # And the run's own chunks did get embedded.
    unembedded = await db_session.scalar(
        select(func.count())
        .select_from(KbChunk)
        .where(KbChunk.entry_id != backlog.entry_id, KbChunk.embedded_at.is_(None))
    )
    assert unembedded == 0


async def test_the_run_flags_near_duplicates_after_it_has_embedded(
    app, client, session_factory, db_session, feed
):
    """Two feed items carrying the same headline and the same text: the second is
    flagged against the first, and both entries survive — flag, never merge."""
    for index in range(2):
        db_session.add(
            FeedItem(
                feed_id=feed.id,
                guid=f"twin-{index}",
                title="Backdoor found in xz utils, tracked as CVE-2024-3094",
                url=f"https://example.test/twin-{index}",
                summary="short",
                content_text=BODY,
            )
        )
    await db_session.commit()
    ids = list(
        (await db_session.execute(select(FeedItem.id).order_by(FeedItem.id))).scalars().all()
    )
    app.dependency_overrides[get_kb_service] = lambda: KbService(
        session_factory=session_factory, embedder=FakeEmbedder()
    )

    response = await client.post("/api/kb/bulk", json={"item_ids": ids, "job_id": "j-dup"})

    done = payloads_for(response.text, "done")[0]
    assert done["saved"] == 2
    assert done["duplicates"] == 1
    entries = (
        (await db_session.execute(select(KbEntry).order_by(KbEntry.id))).scalars().all()
    )
    assert len(entries) == 2
    assert entries[0].possible_duplicate_of is None
    assert entries[1].possible_duplicate_of == entries[0].id


# ----------------------------------------------------- the cap and the cancel


class GatedKb(KbService):
    """A knowledge base whose captures can be counted and held open.

    ``saturated`` fires the moment :data:`MAX_KB_EXTRACTIONS` captures are in
    flight at once, ``landed`` the first time one commits, and ``release`` lets
    the held ones finish. Everything the two timing tests need to say is said with
    events, so neither of them contains a duration.
    """

    __slots__ = ("arrivals", "landed", "live", "peak", "release", "saturated", "hold_from")

    def __init__(self, session_factory, *, transport=None, hold_from: int = 0) -> None:
        super().__init__(session_factory=session_factory, transport=transport)
        self.arrivals = 0
        self.live = 0
        self.peak = 0
        self.hold_from = hold_from
        self.saturated = asyncio.Event()
        self.landed = asyncio.Event()
        self.release = asyncio.Event()

    async def capture_feed_item(self, item_id: int, **kwargs):
        index = self.arrivals
        self.arrivals += 1
        self.live += 1
        self.peak = max(self.peak, self.live)
        if self.live >= MAX_KB_EXTRACTIONS:
            self.saturated.set()
        try:
            if index >= self.hold_from:
                await self.release.wait()
            result = await super().capture_feed_item(item_id, **kwargs)
            self.landed.set()
            return result
        finally:
            self.live -= 1


async def test_the_bulk_job_never_runs_more_than_eight_extractions_at_once(
    app, session_factory, db_session, feed
):
    ids = await seed_items(db_session, feed, MAX_KB_EXTRACTIONS * 3)
    service = GatedKb(session_factory, transport=article_transport(64))
    app.dependency_overrides[get_kb_service] = lambda: service

    async with httpx2.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        stream = asyncio.create_task(
            http.post("/api/kb/bulk", json={"item_ids": ids, "job_id": "j-cap"})
        )
        # The cap is reached; nothing may get past it while the gate is shut.
        await asyncio.wait_for(service.saturated.wait(), timeout=5)
        assert service.live == MAX_KB_EXTRACTIONS
        assert service.arrivals == MAX_KB_EXTRACTIONS
        service.release.set()
        response = await stream

    assert service.peak == MAX_KB_EXTRACTIONS
    assert len(progress(response.text)) == len(ids)
    assert payloads_for(response.text, "done")[0]["saved"] == len(ids)


async def test_at_most_eight_article_fetches_are_in_flight_at_once(
    app, session_factory, db_session, feed
):
    """The cap counted where it is meant to bite: outbound requests.

    The test above pins the semaphore around ``capture_feed_item``, which is a
    property of this job's own loop. The ceiling it exists for is the *sites*' —
    eight parallel reads is neighbourly, eighty is a scrape — so this one holds
    the fetches themselves open and counts how many ``fetch_guarded`` has in
    flight. Items with no stored text, so every one of them really goes out.
    """
    ids = await seed_items(db_session, feed, MAX_KB_EXTRACTIONS * 3, stored_text=False)
    page = fixture_text("article.html")
    live = 0
    peak = 0
    #: Every fetch announces itself here and then waits for a permit, so the test
    #: decides when each one finishes. Nothing waits on a duration, and nothing
    #: waits for something *not* to happen: each step waits for an arrival that a
    #: correct implementation and a broken one both make.
    arrived: asyncio.Queue[None] = asyncio.Queue()
    permits = asyncio.Semaphore(0)

    async def handle(request: httpx2.Request) -> httpx2.Response:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await arrived.put(None)
        try:
            await permits.acquire()
            return httpx2.Response(200, text=page)
        finally:
            live -= 1

    app.dependency_overrides[get_kb_service] = lambda: KbService(
        session_factory=session_factory, transport=httpx2.MockTransport(handle)
    )

    async with httpx2.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        stream = asyncio.create_task(
            http.post("/api/kb/bulk", json={"item_ids": ids, "job_id": "j-fetch"})
        )
        # Eight fetches arrive while no permit has been handed out at all.
        for _ in range(MAX_KB_EXTRACTIONS):
            await asyncio.wait_for(arrived.get(), timeout=5)
        assert live == MAX_KB_EXTRACTIONS
        # From here every further arrival has to be *bought* with a completion:
        # one permit, one more fetch. Without the cap the remaining sixteen would
        # pile up behind the held ones instead, and ``peak`` would say so.
        for _ in range(len(ids) - MAX_KB_EXTRACTIONS):
            permits.release()
            await asyncio.wait_for(arrived.get(), timeout=5)
        for _ in range(MAX_KB_EXTRACTIONS):
            permits.release()
        response = await stream

    assert peak == MAX_KB_EXTRACTIONS
    assert payloads_for(response.text, "done")[0]["saved"] == len(ids)


async def test_cancelling_mid_run_leaves_committed_entries_committed(
    app, session_factory, db_session, feed
):
    """Stop is instant and honest: an ``error(cancelled)`` frame, a ``done`` that
    names what actually landed, and the entries captured before it still there."""
    ids = await seed_items(db_session, feed, 12)
    service = GatedKb(session_factory, transport=article_transport(64), hold_from=1)
    app.dependency_overrides[get_kb_service] = lambda: service

    async with httpx2.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        stream = asyncio.create_task(
            http.post("/api/kb/bulk", json={"item_ids": ids, "job_id": "j-stop"})
        )
        # One item is through; the other eleven are holding the gate.
        await asyncio.wait_for(service.landed.wait(), timeout=5)
        cancelled = await http.post("/api/kb/bulk/cancel", json={"job_id": "j-stop"})
        assert cancelled.json() == {"cancelled": True}
        response = await stream

    names = event_names(response.text)
    assert names[0] == "turn_start"
    assert names[-1] == "done"
    error = next(
        payload for payload in payloads_for(response.text, "error")
    )
    assert error["type"] == "cancelled"
    assert error["message"] == CANCELLED_MESSAGE

    done = payloads_for(response.text, "done")[0]
    assert done["saved"] == 1
    assert len(done["entry_ids"]) == 1

    # The committed entry is committed, and nothing else was written.
    entries = (await db_session.execute(select(KbEntry))).scalars().all()
    assert [entry.id for entry in entries] == done["entry_ids"]


async def test_cancelling_nothing_is_a_two_hundred_with_false(client, kb):
    response = await client.post("/api/kb/bulk/cancel", json={"job_id": "never-started"})
    assert response.status_code == 200
    assert response.json() == {"cancelled": False}


# --------------------------------------------------------------- acceptance


async def test_twenty_items_stream_progress_and_then_done(client, kb, db_session, feed):
    """P2-4's acceptance, in pytest: select twenty, save, watch it land.

    The items carry no stored text, so all twenty go out through the mock
    transport and ``fetch_guarded`` — the path the browser acceptance walks.
    """
    ids = await seed_items(db_session, feed, 20, stored_text=False)

    response = await client.post("/api/kb/bulk", json={"item_ids": ids, "job_id": "j-20"})

    items = progress(response.text)
    assert len(items) == 20
    assert [row["done"] for row in items] == list(range(1, 21))
    done = payloads_for(response.text, "done")[0]
    assert done["saved"] == 20
    assert sorted(done["entry_ids"]) == sorted(row["entry_id"] for row in items)
    assert await db_session.scalar(select(func.count()).select_from(KbEntry)) == 20


async def test_a_job_id_is_generated_when_the_client_sends_none(client, kb, db_session, feed):
    ids = await seed_items(db_session, feed, 1)

    response = await client.post("/api/kb/bulk", json={"item_ids": ids})

    started = parse_sse(response.text)[0][1]
    assert started["job_id"]
    assert payloads_for(response.text, "done")[0]["saved"] == 1


async def test_an_empty_or_oversized_selection_is_refused(client, kb):
    assert (await client.post("/api/kb/bulk", json={"item_ids": []})).status_code == 422
    too_many = {"item_ids": list(range(1, 202))}
    assert (await client.post("/api/kb/bulk", json=too_many)).status_code == 422


async def test_a_bulk_run_never_auto_compiles(app, client, session_factory, db_session, feed):
    """``kb_compile_mode: auto`` compiles one capture, not two hundred.

    Auto-compile is what one click on one article buys. "Save all" is one click
    too, and answering it with an Anthropic call per item is the surprise the
    monthly budget exists to prevent — compiling a selection stays the explicit
    ``POST /api/kb/compile``, which prices itself first.
    """
    async with session_factory() as session:
        await settings_service.set_many(
            session, {"anthropic_api_key": "sk-ant-test", "kb_compile_mode": "auto"}
        )
        await session.commit()
    # An empty script: a compile would raise. ``guarded`` swallows that, so the
    # load-bearing assertion is the empty call list below — the raise only makes
    # a stray call impossible to mistake for a no-op.
    anthropic = ScriptedAnthropic([])
    service = KbService(
        session_factory=session_factory,
        transport=article_transport(64),
        client_factory=lambda _key: anthropic,
    )
    app.dependency_overrides[get_kb_service] = lambda: service
    ids = await seed_items(db_session, feed, 3)

    response = await client.post("/api/kb/bulk", json={"item_ids": ids, "job_id": "j-auto"})

    assert payloads_for(response.text, "done")[0]["saved"] == 3
    assert anthropic.calls == []
    compiled = await db_session.scalar(
        select(func.count()).select_from(KbEntry).where(KbEntry.compiled_at.isnot(None))
    )
    assert compiled == 0
