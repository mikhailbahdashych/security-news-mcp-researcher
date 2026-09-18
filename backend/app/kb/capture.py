"""Writing to the knowledge base: capture, snapshots, refresh, delete, merge.

Everything in here is a **consequence of a user action** — there is no crawler and
nothing runs on a timer. What the module owns is the order those consequences
happen in, because most of the rules that matter are ordering rules:

* **Dedup before length.** An article that is already captured is already
  captured, however short the text that arrived this time.
* **``published_at`` is the source's date or nothing.** Never the capture time:
  "when did we first read this" is ``captured_at``, and conflating the two makes
  every back-fill look like today's news.
* **No transaction is held across an embedding call.** SQLite has one writer, and
  a capture that kept its write transaction open while it waited on an HTTPS round
  trip would lock the rest of the application out for the duration.
* **A snapshot is never destroyed.** A refresh that fails keeps the text it was
  going to replace; a soft delete drops the derived chunks and leaves the
  snapshots, because Undo re-chunks from them. A deleted entry stays chunkless
  even when its source is edited afterwards — "deleted" is the absence of chunks,
  and re-creating one would be a chunk nothing could ever embed or reach.

The caller owns nothing here: every function takes the session factory and opens
its own short transactions.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import httpx2
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Note, utcnow
from app.kb.chunking import estimate_tokens, split_markdown
from app.kb.embeddings import Embedder, max_tokens_for, plan_batches
from app.kb.entities import extract_entities
from app.kb.models import (
    KbActivity,
    KbChunk,
    KbEntry,
    KbEntryEntity,
    KbEntryLink,
    KbEntryTag,
    KbEntryTopic,
    KbSnapshot,
)
from app.kb.store import (
    KnowledgeStore,
    SearchFilters,
    SqliteKnowledgeStore,
    VectorRow,
    published_day,
)
from app.kb.urls import canonical_url
from app.services import extract as extract_service

logger = logging.getLogger(__name__)

#: Below this many characters a snapshot is a cookie banner, a stub or a teaser,
#: not an article. Overridable through the ``kb_min_snapshot_chars`` setting.
DEFAULT_MIN_SNAPSHOT_CHARS = 400

#: The activity log is a trail, not an archive; it is pruned to this many rows on
#: every write. Read at call time so a test can lower it.
ACTIVITY_MAX_ROWS = 10_000

#: What separates two entries' notes when they are merged.
NOTE_SEPARATOR = "\n\n---\n\n"

#: The three ways a capture writes nothing, as ``CaptureResult.skipped_code``.
SKIP_NOT_A_URL = "not_a_url"
SKIP_FETCH_FAILED = "fetch_failed"
SKIP_TOO_SHORT = "too_short"

#: The title similarity a near-duplicate needs as well as a close vector
#: (spec §4.5). Fixed, unlike the cosine threshold, which is a setting.
TITLE_TRIGRAM_MIN = 0.8

#: The cosine a near-duplicate needs — the default of ``kb_duplicate_threshold``.
#: **Unvalidated** (spec §9): calibrate it on the first 200 entries.
DEFAULT_DUPLICATE_THRESHOLD = 0.92

#: How many neighbours the near-duplicate KNN asks for. A duplicate that is not
#: in the nearest handful is not a duplicate.
NEAR_DUPLICATE_K = 5

#: How many titles the *embedder-less* leg compares against. There is no index on
#: trigram similarity and no FTS5 table over titles, so this is a scan.
#: ponytail: newest-N scan; give it an index only if a knowledge base ever gets
#: big enough for the scan to show up.
NEAR_DUPLICATE_TITLE_SCAN = 2_000


class KbConflict(Exception):
    """An operation the current state of the knowledge base refuses.

    Undo of a deleted entry whose URL has since been re-captured, or a purge that
    names an entry which is not deleted. Both are the user's to resolve, so they
    surface as a 409 rather than a 500.
    """


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """What one capture did.

    ``created`` distinguishes the two successes — a new entry, or an existing one
    that got a back-link — which is exactly the 201/200 split the API makes.
    ``skipped_reason`` is set only when nothing was written at all, and
    ``skipped_code`` names *which* refusal it was: the sentence is for the user,
    the code is what the API turns into a status. "You typed it wrong", "the site
    did not answer" and "the page was too short" are three different things, and
    a client that gets one status code for all three can only guess.
    """

    entry_id: int | None
    created: bool
    skipped_reason: str | None = None
    skipped_code: str | None = None
    possible_duplicate_of: int | None = None


#: The three outcomes of a re-read. ``changed`` alone collapses the last two.
RefreshStatus = Literal["updated", "unchanged", "failed"]


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """What re-reading an entry's source found.

    ``version`` is the version that is current *afterwards*, so an unchanged page
    and a failed fetch both report the version the entry already had.
    """

    entry_id: int
    changed: bool
    version: int
    reason: str | None = None

    @property
    def status(self) -> RefreshStatus:
        """``updated`` | ``unchanged`` | ``failed`` — the three outcomes, named.

        A failed fetch and an unchanged page are both ``changed=False``, and a
        client that has only that flag has no way to tell "the source has not
        moved" from "Cloudflare refused us". A ``reason`` is only ever set on the
        paths that did not read the source.
        """
        if self.changed:
            return "updated"
        return "failed" if self.reason else "unchanged"


def normalise_text(text: str | None) -> str:
    """The form the content hash is taken over: lower-cased, whitespace-collapsed."""
    return " ".join((text or "").split()).lower()


def content_hash(text: str | None) -> str:
    """The dedup key for text that has no URL — ``sha256`` of the normalised form.

    Normalised, because the same article re-fetched a week later differs by
    whitespace and nothing else. Distinct from :func:`snapshot_hash`, which is
    taken over the text *verbatim* because a refresh must notice a change the
    normalisation would hide.
    """
    return hashlib.sha256(normalise_text(text).encode("utf-8")).hexdigest()


def snapshot_hash(text: str | None) -> str:
    """``sha256`` of the snapshot text exactly as stored."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# ----------------------------------------------------------------- activity


async def log_activity(
    session: AsyncSession,
    action: str,
    *,
    entry_id: int | None = None,
    source: str = "",
    model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    detail: str | None = None,
) -> KbActivity:
    """Append one row to the trail and prune it back to its ceiling.

    The prune runs on write rather than on a timer because there is no timer in
    this application; a ``count(*)`` over at most ten thousand rows is cheaper
    than the alternative of an unbounded table nobody ever looks at.
    """
    row = KbActivity(
        action=action,
        entry_id=entry_id,
        source=source,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        detail=detail,
    )
    session.add(row)
    await session.flush()
    await _prune_activity(session)
    return row


async def _prune_activity(session: AsyncSession) -> None:
    total = await session.scalar(select(func.count()).select_from(KbActivity)) or 0
    if total <= ACTIVITY_MAX_ROWS:
        return
    oldest_kept = await session.scalar(
        select(KbActivity.id).order_by(KbActivity.id.desc()).offset(ACTIVITY_MAX_ROWS - 1).limit(1)
    )
    if oldest_kept is not None:
        await session.execute(delete(KbActivity).where(KbActivity.id < oldest_kept))


# ------------------------------------------------------------------ capture


async def capture_article(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    *,
    url: str | None,
    title: str,
    source_name: str | None = None,
    text: str,
    published_at: datetime | None = None,
    feed_item_id: int | None = None,
    captured_by: str = "auto",
    kind: str = "article",
    authorship: str = "source",
    note_id: int | None = None,
    session_id: int | None = None,
    turn_message_id: int | None = None,
    source_ref: str | None = None,
    lang: str | None = None,
    min_chars: int = DEFAULT_MIN_SNAPSHOT_CHARS,
    trigger: str = "manual",
    duplicate_threshold: float = DEFAULT_DUPLICATE_THRESHOLD,
    defer_embedding: bool = False,
) -> CaptureResult:
    """Capture one piece of text as an entry, or recognise it as one already held.

    *published_at* is the **source's** date. Callers pass the feed item's date,
    or the extractor's if it exposes one, or nothing — never ``utcnow()``.

    *duplicate_threshold* is passed in rather than read here, like *min_chars*:
    settings are the caller's to resolve, and a bulk run would otherwise read the
    same row once per item.

    *defer_embedding* hands both pieces of vector work — the embed and the
    near-duplicate check that needs its result — to the caller. It exists for the
    bulk job (:mod:`app.kb.bulk`), where embedding per entry would be one Voyage
    call and one ``kb_activity`` row per article instead of one of each per run.
    A caller that sets it **must** embed and flag afterwards, or the entry is
    left keyword-only and unchecked.
    """
    canonical = canonical_url(url)
    body = (text or "").strip()
    digest = content_hash(body)

    held: tuple[int, bool, int | None] | None = None
    async with session_factory() as session:
        existing = await _find_duplicate(
            session,
            url=canonical,
            feed_item_id=feed_item_id,
            note_id=note_id,
            digest=digest,
        )
        if existing is not None:
            added = await _add_backlink(
                session,
                existing.id,
                feed_item_id=feed_item_id,
                note_id=note_id,
                session_id=session_id,
            )
            was_deleted = existing.deleted_at is not None
            await log_activity(
                session,
                "capture",
                entry_id=existing.id,
                source=trigger,
                detail=(
                    "already captured"
                    + ("; back-link added" if added else "")
                    + ("; restored from the trash" if was_deleted else "")
                ),
            )
            await session.commit()
            held = (existing.id, was_deleted, existing.possible_duplicate_of)

    if held is not None:
        entry_id, was_deleted, duplicate_of = held
        # Save means "I want this", so saving something that is sitting in the
        # trash brings it back rather than reporting a success that changed
        # nothing. Only the `feed_item_id` / `note_id` legs of the dedup can
        # return a deleted entry — their uniqueness indexes have no `deleted_at`
        # scope — and this runs outside the session above because `undelete`
        # opens its own and re-embeds after it.
        if was_deleted:
            await undelete(session_factory, embedder, entry_id, trigger=trigger)
        return CaptureResult(entry_id=entry_id, created=False, possible_duplicate_of=duplicate_of)

    async with session_factory() as session:
        if len(body) < min_chars:
            reason = f"only {len(body)} characters of text; the minimum is {min_chars}"
            await log_activity(session, "skip", source=trigger, detail=reason)
            await session.commit()
            return CaptureResult(
                entry_id=None, created=False, skipped_reason=reason, skipped_code=SKIP_TOO_SHORT
            )

        entry = KbEntry(
            kind=kind,
            title=(title or "").strip() or canonical or "Untitled",
            url=canonical,
            source_name=source_name,
            authorship=authorship,
            lang=lang,
            feed_item_id=feed_item_id,
            note_id=note_id,
            session_id=session_id,
            turn_message_id=turn_message_id,
            source_ref=source_ref,
            content_hash=digest,
            published_at=published_at,
            captured_by=captured_by,
        )
        session.add(entry)
        await session.flush()

        snapshot = await _add_snapshot(session, entry, body, version=1)
        entry.current_snapshot_id = snapshot.id
        await _rechunk(session, entry, snapshot, body)
        await _sync_regex_entities(session, entry.id, f"{entry.title}\n{body}")
        await _add_backlink(
            session,
            entry.id,
            feed_item_id=feed_item_id,
            note_id=note_id,
            session_id=session_id,
        )
        await log_activity(
            session,
            "capture",
            entry_id=entry.id,
            source=trigger,
            detail=f"{len(body)} characters",
        )
        await session.commit()
        entry_id = entry.id

    if defer_embedding:
        return CaptureResult(entry_id=entry_id, created=True)

    # Outside every transaction, on purpose — see the module docstring.
    await _embed_pending_quietly(session_factory, embedder, entry_id, trigger=trigger)
    # After the embed, because the check reads the vector that embed just wrote.
    return CaptureResult(
        entry_id=entry_id,
        created=True,
        possible_duplicate_of=await flag_near_duplicate(
            session_factory, embedder, entry_id, threshold=duplicate_threshold, trigger=trigger
        ),
    )


async def capture_note(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    note_id: int,
    *,
    min_chars: int = DEFAULT_MIN_SNAPSHOT_CHARS,
    trigger: str = "note",
    duplicate_threshold: float = DEFAULT_DUPLICATE_THRESHOLD,
) -> CaptureResult:
    """Capture a note's body, or bring its existing entry up to date.

    Unlike an article a note has a single authoritative source inside this
    application, so a second capture of the same note is not a duplicate to
    ignore — it is an edit, and the entry gets a new snapshot version.
    """
    async with session_factory() as session:
        note = await session.get(Note, note_id)
        if note is None:
            raise LookupError(f"No note with id {note_id}")
        title = (note.title or "").strip() or f"Note {note.id}"
        body = (note.body_md or "").strip()
        # Copied out here, not read after the block: the session closes below and
        # a detached instance is one setting away from raising.
        note_session_id = note.session_id
        existing = await session.scalar(select(KbEntry).where(KbEntry.note_id == note_id))
        existing_id = existing.id if existing is not None else None

    if existing_id is not None:
        result = await _replace_snapshot(
            session_factory, existing_id, body, title=title, trigger=trigger
        )
        await _embed_pending_quietly(session_factory, embedder, existing_id, trigger=trigger)
        return CaptureResult(entry_id=existing_id, created=False, skipped_reason=result.reason)

    return await capture_article(
        session_factory,
        embedder,
        url=None,
        title=title,
        source_name=None,
        text=body,
        published_at=None,
        note_id=note_id,
        session_id=note_session_id,
        source_ref=f"note {note_id}: {title}",
        kind="note",
        authorship="human",
        captured_by="auto",
        min_chars=min_chars,
        trigger=trigger,
        duplicate_threshold=duplicate_threshold,
    )


async def capture_url(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    url: str,
    *,
    title: str | None = None,
    captured_by: str = "user",
    kind: str = "manual",
    min_chars: int = DEFAULT_MIN_SNAPSHOT_CHARS,
    timeout_s: int = 15,
    trigger: str = "url",
    transport: httpx2.AsyncBaseTransport | None = None,
    duplicate_threshold: float = DEFAULT_DUPLICATE_THRESHOLD,
) -> CaptureResult:
    """Fetch *url* and capture what the extractor makes of it.

    The fetch is ``extract_service.extract_article``, which goes through
    ``url_guard.fetch_guarded`` with every hop validated and the body ceiling
    applied. There must never be a second HTTP path around the guard.

    The kind is ``manual``, not ``article``: this is the user deciding to keep
    something, and the Knowledge page separates what they saved by hand from what
    arrived in a feed. The title comes off the page — the extractor's metadata,
    which falls back to ``<title>``, then the first Markdown heading, then the URL
    — because a list of URLs is a list nobody can scan. A title the user typed
    always wins.
    """
    canonical = canonical_url(url)
    if canonical is None:
        reason = f"{url!r} is not an absolute http(s) URL"
        await _log_in_new_session(session_factory, "skip", source=trigger, detail=reason)
        return CaptureResult(
            entry_id=None, created=False, skipped_reason=reason, skipped_code=SKIP_NOT_A_URL
        )

    result = await extract_service.extract_article(
        canonical, timeout_s=timeout_s, transport=transport
    )
    if not result.ok or not result.text:
        reason = result.reason or "no readable content"
        await _log_in_new_session(
            session_factory, "skip", source=trigger, detail=f"{canonical}: {reason}"
        )
        return CaptureResult(
            entry_id=None, created=False, skipped_reason=reason, skipped_code=SKIP_FETCH_FAILED
        )

    return await capture_article(
        session_factory,
        embedder,
        url=canonical,
        title=title or result.title or _title_from(result.text, canonical),
        source_name=None,
        text=result.text,
        # The extractor yields text and a title only; it exposes no publication
        # date, so there is nothing honest to put here.
        published_at=None,
        captured_by=captured_by,
        kind=kind,
        min_chars=min_chars,
        trigger=trigger,
        duplicate_threshold=duplicate_threshold,
    )


def _title_from(text: str, fallback: str) -> str:
    """The first Markdown heading of an extracted article, else its URL."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            if heading:
                return heading[:300]
        if stripped:
            break
    return fallback


# ------------------------------------------------------------------ refresh


async def refresh_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    entry_id: int,
    *,
    timeout_s: int = 15,
    transport: httpx2.AsyncBaseTransport | None = None,
    trigger: str = "refresh",
) -> RefreshResult:
    """Re-read an entry's source and store a new version if the text moved.

    A failure is a reason and an activity row, never a lost snapshot: the whole
    point of versioning the text is that the copy in hand is worth more than the
    copy that could not be fetched.
    """
    async with session_factory() as session:
        entry = await session.get(KbEntry, entry_id)
        if entry is None:
            raise LookupError(f"No knowledge-base entry with id {entry_id}")
        url = entry.url
        version = await _current_version(session, entry_id)
        if not url:
            reason = "this entry has no URL to re-read"
            await log_activity(session, "skip", entry_id=entry_id, source=trigger, detail=reason)
            await session.commit()
            return RefreshResult(entry_id=entry_id, changed=False, version=version, reason=reason)

    result = await extract_service.extract_article(url, timeout_s=timeout_s, transport=transport)
    if not result.ok or not result.text:
        reason = result.reason or "no readable content"
        await _log_in_new_session(
            session_factory, "skip", entry_id=entry_id, source=trigger, detail=reason
        )
        return RefreshResult(entry_id=entry_id, changed=False, version=version, reason=reason)

    outcome = await _replace_snapshot(
        session_factory, entry_id, result.text.strip(), trigger=trigger
    )
    if outcome.changed:
        await _embed_pending_quietly(session_factory, embedder, entry_id, trigger=trigger)
    return outcome


async def _replace_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    entry_id: int,
    body: str,
    *,
    title: str | None = None,
    trigger: str,
) -> RefreshResult:
    """Store *body* as the next version, unless it is byte-identical to the current one.

    A **deleted** entry still gets its snapshot — the text is worth keeping, and
    the note it came from is the authority on its own body — but it gets no
    chunks. ``soft_delete`` drops them precisely so that "deleted" needs no filter
    anywhere; a chunk created here would also be one ``embed_pending`` skips
    forever, so the "N chunks not embedded" badge could never clear. ``undelete``
    re-chunks from the current snapshot, which is this one.
    """
    async with session_factory() as session:
        entry = await session.get(KbEntry, entry_id)
        if entry is None:
            raise LookupError(f"No knowledge-base entry with id {entry_id}")
        current = (
            await session.get(KbSnapshot, entry.current_snapshot_id)
            if entry.current_snapshot_id
            else None
        )
        version = current.version if current is not None else 0
        if title and title != entry.title:
            entry.title = title
        if current is not None and current.sha256 == snapshot_hash(body):
            await session.commit()
            return RefreshResult(entry_id=entry_id, changed=False, version=version)

        snapshot = await _add_snapshot(session, entry, body, version=version + 1)
        entry.current_snapshot_id = snapshot.id
        entry.content_hash = content_hash(body)
        entry.updated_at = utcnow()
        if entry.deleted_at is None:
            await _rechunk(session, entry, snapshot, body)
        await _sync_regex_entities(session, entry.id, f"{entry.title}\n{body}")
        await log_activity(
            session,
            "capture",
            entry_id=entry_id,
            source=trigger,
            detail=f"version {snapshot.version}, {len(body)} characters",
        )
        await session.commit()
        return RefreshResult(entry_id=entry_id, changed=True, version=snapshot.version)


# -------------------------------------------------------- delete and revive


async def soft_delete(
    session_factory: async_sessionmaker[AsyncSession], entry_id: int, *, trigger: str = "user"
) -> None:
    """Hide an entry and drop its chunks; the snapshots stay for Undo.

    Dropping the chunks is what removes the entry from both search legs — the FTS
    rows and the vectors go with them, by trigger — so there is no filter anywhere
    that can be forgotten. The other half of that rule lives in
    :func:`_replace_snapshot`, which does not give them back.
    """
    async with session_factory() as session:
        entry = await session.get(KbEntry, entry_id)
        if entry is None:
            raise LookupError(f"No knowledge-base entry with id {entry_id}")
        if entry.deleted_at is None:
            entry.deleted_at = utcnow()
            await session.execute(delete(KbChunk).where(KbChunk.entry_id == entry_id))
            await log_activity(session, "delete", entry_id=entry_id, source=trigger)
        await session.commit()


async def undelete(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    entry_id: int,
    *,
    trigger: str = "user",
) -> None:
    """Bring a soft-deleted entry back and re-chunk it from its current snapshot.

    The URL uniqueness index is scoped to live entries, so an Undo can collide
    with a fresh capture of the same URL made in the meantime. That is a conflict
    for the user to resolve (merge, or delete the new one), not something to
    resolve by guessing.
    """
    async with session_factory() as session:
        entry = await session.get(KbEntry, entry_id)
        if entry is None:
            raise LookupError(f"No knowledge-base entry with id {entry_id}")
        if entry.deleted_at is None:
            return
        snapshot = (
            await session.get(KbSnapshot, entry.current_snapshot_id)
            if entry.current_snapshot_id
            else None
        )
        if snapshot is not None:
            await _rechunk(session, entry, snapshot, snapshot.text)
        await log_activity(session, "undelete", entry_id=entry_id, source=trigger)
        # Clearing the flag is the very last thing before the commit: any flush
        # before this point would carry the revived URL into the partial unique
        # index, and the conflict would surface as an autoflush from somewhere
        # unrelated rather than from this commit.
        entry.deleted_at = None
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise KbConflict(
                "This entry's URL has been captured again since it was deleted. "
                "Merge the two, or delete the newer one first."
            ) from exc

    await _embed_pending_quietly(session_factory, embedder, entry_id, trigger=trigger)


async def purge(session_factory: async_sessionmaker[AsyncSession], ids: Sequence[int]) -> int:
    """Permanently remove soft-deleted entries. The only irreversible action here.

    An id that is not deleted is refused rather than deleted anyway: purge is
    reached from a Settings button with a count, and "N entries" must never
    quietly include one the user can still see.
    """
    wanted = list(dict.fromkeys(int(entry_id) for entry_id in ids))
    if not wanted:
        return 0
    async with session_factory() as session:
        rows = (
            (await session.execute(select(KbEntry).where(KbEntry.id.in_(wanted)))).scalars().all()
        )
        alive = [row.id for row in rows if row.deleted_at is None]
        if alive:
            raise KbConflict(
                "Only deleted entries can be purged; "
                f"{', '.join(str(entry_id) for entry_id in alive)} are not deleted."
            )
        for row in rows:
            await session.delete(row)
        await log_activity(session, "delete", source="purge", detail=f"purged {len(rows)} entries")
        await session.commit()
        return len(rows)


# -------------------------------------------------------------------- merge


async def merge_entries(
    session_factory: async_sessionmaker[AsyncSession], keep_id: int, drop_id: int
) -> int:
    """Fold one entry into the other and return the id of the one that survives.

    **The older entry is always the one kept**, whichever way round the arguments
    arrive: its id is what back-links, notes and citations elsewhere already point
    at, and the point of a merge is that those keep working. The newer entry is
    soft-deleted, so the merge is as reversible as any other delete.
    """
    if keep_id == drop_id:
        return keep_id
    async with session_factory() as session:
        first = await session.get(KbEntry, keep_id)
        second = await session.get(KbEntry, drop_id)
        if first is None or second is None:
            missing = keep_id if first is None else drop_id
            raise LookupError(f"No knowledge-base entry with id {missing}")

        kept, dropped = sorted((first, second), key=lambda entry: (entry.captured_at, entry.id))

        await _union_entities(session, kept.id, dropped.id)
        await _union_topics(session, kept.id, dropped.id)
        await _union_tags(session, kept.id, dropped.id)
        await _union_links(session, kept, dropped)

        notes = [part for part in (kept.notes_md, dropped.notes_md) if (part or "").strip()]
        kept.notes_md = NOTE_SEPARATOR.join(notes)
        if kept.possible_duplicate_of == dropped.id:
            kept.possible_duplicate_of = None
        kept.updated_at = utcnow()

        if dropped.deleted_at is None:
            dropped.deleted_at = utcnow()
            await session.execute(delete(KbChunk).where(KbChunk.entry_id == dropped.id))
        dropped.possible_duplicate_of = kept.id

        await log_activity(
            session,
            "merge",
            entry_id=kept.id,
            source="user",
            detail=f"merged entry {dropped.id} into {kept.id}",
        )
        await session.commit()
        return kept.id


async def _union_entities(session: AsyncSession, keep_id: int, drop_id: int) -> None:
    held = {
        (kind, value)
        for kind, value in (
            await session.execute(
                select(KbEntryEntity.kind, KbEntryEntity.value).where(
                    KbEntryEntity.entry_id == keep_id
                )
            )
        ).all()
    }
    rows = (
        (await session.execute(select(KbEntryEntity).where(KbEntryEntity.entry_id == drop_id)))
        .scalars()
        .all()
    )
    for row in rows:
        if (row.kind, row.value) not in held:
            session.add(
                KbEntryEntity(entry_id=keep_id, kind=row.kind, value=row.value, source=row.source)
            )
            held.add((row.kind, row.value))


async def _union_topics(session: AsyncSession, keep_id: int, drop_id: int) -> None:
    held = set(
        (
            await session.execute(
                select(KbEntryTopic.topic_id).where(KbEntryTopic.entry_id == keep_id)
            )
        )
        .scalars()
        .all()
    )
    rows = (
        (await session.execute(select(KbEntryTopic).where(KbEntryTopic.entry_id == drop_id)))
        .scalars()
        .all()
    )
    for row in rows:
        if row.topic_id not in held:
            session.add(
                KbEntryTopic(entry_id=keep_id, topic_id=row.topic_id, suggested=row.suggested)
            )
            held.add(row.topic_id)


async def _union_tags(session: AsyncSession, keep_id: int, drop_id: int) -> None:
    held = set(
        (await session.execute(select(KbEntryTag.tag).where(KbEntryTag.entry_id == keep_id)))
        .scalars()
        .all()
    )
    rows = (
        (await session.execute(select(KbEntryTag).where(KbEntryTag.entry_id == drop_id)))
        .scalars()
        .all()
    )
    for row in rows:
        if row.tag not in held:
            session.add(KbEntryTag(entry_id=keep_id, tag=row.tag, suggested=row.suggested))
            held.add(row.tag)


async def _union_links(session: AsyncSession, kept: KbEntry, dropped: KbEntry) -> None:
    """Move the dropped entry's back-links, plus what its own source columns named.

    The source columns themselves cannot move — ``feed_item_id`` and ``note_id``
    are uniquely indexed without a ``deleted_at`` scope, so the dropped row keeps
    holding them — but the *information* can, as an ordinary link.
    """
    existing = {
        (session_id, note_id, feed_item_id)
        for session_id, note_id, feed_item_id in (
            await session.execute(
                select(KbEntryLink.session_id, KbEntryLink.note_id, KbEntryLink.feed_item_id).where(
                    KbEntryLink.entry_id == kept.id
                )
            )
        ).all()
    }
    rows = (
        (await session.execute(select(KbEntryLink).where(KbEntryLink.entry_id == dropped.id)))
        .scalars()
        .all()
    )
    candidates = [(row.session_id, row.note_id, row.feed_item_id) for row in rows]
    candidates.append((dropped.session_id, dropped.note_id, dropped.feed_item_id))
    for session_id, note_id, feed_item_id in candidates:
        key = (session_id, note_id, feed_item_id)
        if key in existing or key == (None, None, None):
            continue
        session.add(
            KbEntryLink(
                entry_id=kept.id,
                session_id=session_id,
                note_id=note_id,
                feed_item_id=feed_item_id,
            )
        )
        existing.add(key)
    await session.execute(delete(KbEntryLink).where(KbEntryLink.entry_id == dropped.id))


# ---------------------------------------------------------------- embedding


async def embed_pending(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    *,
    entry_id: int | None = None,
    limit: int = 500,
    source: str = "",
) -> int:
    """Embed chunks that have no vector yet, and return how many were embedded.

    ``embedded_at IS NULL`` is the one definition of "pending", so this is also
    what Re-index resumes from. With an embedder that cannot embed —
    :class:`~app.kb.embeddings.NullEmbedder`, used whenever no Voyage key is
    configured — it is a no-op and the chunks stay pending, which is a normal
    state of the knowledge base rather than a failure.

    **Written per batch, not once at the end.** The selection is grouped by
    :func:`~app.kb.embeddings.plan_batches` at the configured model's own ceiling
    (:func:`~app.kb.embeddings.max_tokens_for`), so that one group is exactly one
    request the embedder sends — and each group's vectors, ``embedded_at`` and
    ``embedding_model`` are committed before the next group is sent. So a provider
    that 429s on the fifth request leaves exactly the chunks it never reached
    pending, the entry's "8 of 11" is derived from counting them, and the next run
    resumes from there instead of re-embedding what was already paid for.

    Whatever it did manage is counted: one ``kb_activity`` row per call, with the
    model and the estimated token count of the batches that landed, written even
    when a later batch raised. The estimate is ``ceil(chars / 3.6)`` — the same one
    the batcher packs with — because the count has to be attributable to the chunks
    it was spent on, which a provider's reply about one request is not.
    """
    if embedder is None or embedder.dimensions <= 0:
        return 0

    async with session_factory() as session:
        statement = (
            select(
                KbChunk.id,
                KbChunk.text,
                KbChunk.kind,
                KbEntry.id,
                KbEntry.kind,
                KbEntry.review_status,
                KbEntry.authorship,
                KbEntry.published_at,
                KbEntry.captured_at,
            )
            .join(KbEntry, KbEntry.id == KbChunk.entry_id)
            .where(KbChunk.embedded_at.is_(None), KbEntry.deleted_at.is_(None))
            .order_by(KbChunk.id)
            .limit(limit)
        )
        if entry_id is not None:
            statement = statement.where(KbChunk.entry_id == entry_id)
        rows = (await session.execute(statement)).all()

    if not rows:
        return 0

    store = SqliteKnowledgeStore(session_factory)
    embedded = 0
    tokens = 0
    try:
        for group in plan_batches(
            [row[1] for row in rows], max_tokens=max_tokens_for(embedder.model)
        ):
            batch = [rows[index] for index in group]
            # No session is open here: an embedder call is a network round trip,
            # and SQLite has exactly one writer.
            vectors = await embedder.embed_documents([row[1] for row in batch])

            await store.upsert_vectors(
                [
                    VectorRow(
                        chunk_id=row[0],
                        entry_id=row[3],
                        entry_kind=row[4],
                        chunk_kind=row[2],
                        reviewed=row[5] == "reviewed",
                        authorship=row[6],
                        published_day=published_day(row[7] or row[8]),
                        embedding=vector,
                    )
                    for row, vector in zip(batch, vectors, strict=True)
                ]
            )

            embedded_at = utcnow()
            async with session_factory() as session:
                for row in batch:
                    chunk = await session.get(KbChunk, row[0])
                    if chunk is not None:
                        chunk.embedded_at = embedded_at
                        chunk.embedding_model = embedder.model
                await session.commit()
            embedded += len(batch)
            tokens += sum(estimate_tokens(row[1]) for row in batch)
    finally:
        if embedded:
            await _log_in_new_session(
                session_factory,
                "embed",
                entry_id=entry_id,
                source=source,
                model=embedder.model,
                input_tokens=tokens,
                detail=f"{embedded} chunks",
            )
    return embedded


async def _embed_pending_quietly(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    entry_id: int,
    *,
    trigger: str,
) -> int:
    """Embed what is pending, and turn a failure into a row in the trail.

    Every caller runs this **after** its own commit, so the entry is already in
    the database and there is nothing left to roll back. Spec §5 is explicit that
    a capture completes and the chunks that could not be reached stay pending —
    an embedding provider being down is not a reason to answer a successful save
    with a 500, and ``embedded_at IS NULL`` is exactly what Re-index resumes from.
    """
    try:
        return await embed_pending(
            session_factory, embedder, entry_id=entry_id, source=trigger
        )
    except Exception as exc:  # noqa: BLE001 - the capture already committed
        logger.exception("Embedding entry %s failed", entry_id)
        await _log_in_new_session(
            session_factory,
            "skip",
            entry_id=entry_id,
            source=trigger,
            detail=f"embedding failed, chunks stay pending: {type(exc).__name__}: {exc}",
        )
        return 0


# ----------------------------------------------------- near-duplicates


@dataclass(frozen=True, slots=True)
class DuplicateMatch:
    """The older entry a capture looks like, and how much it looked like it.

    The scores travel with the id so the activity row can name both of them: a
    threshold nobody can see the evidence for is a threshold nobody can calibrate,
    and ``kb_duplicate_threshold``'s 0.92 is explicitly unvalidated (spec §9).
    """

    entry_id: int
    cosine: float | None
    title_score: float


def trigrams(value: str) -> set[str]:
    """Character trigrams of the normalised text, padded the way ``pg_trgm`` does.

    The padding (two spaces in front, one behind) is what gives a two-word title
    enough trigrams to score against, and it makes the first and last characters
    count as much as the middle ones.
    """
    cleaned = normalise_text(value)
    if not cleaned:
        return set()
    padded = f"  {cleaned} "
    return {padded[index : index + 3] for index in range(len(padded) - 2)}


def title_similarity(a: str, b: str) -> float:
    """Dice coefficient over character trigrams: ``1.0`` identical, ``0.0`` unrelated.

    Dice rather than Jaccard because the interesting case is a title that gained
    or lost a few words ("The xz backdoor" / "The xz backdoor, explained"), and
    Jaccard punishes that twice over.
    """
    left, right = trigrams(a), trigrams(b)
    if not left or not right:
        return 0.0
    return 2 * len(left & right) / (len(left) + len(right))


def cosine_from_distance(distance: float) -> float:
    """vec0's ``distance`` as a cosine similarity, for **unit** vectors.

    ``kb_chunk_vec`` is created without ``distance_metric=``, and sqlite-vec's
    default is **L2**, not cosine — so the conversion is ``1 - d²/2`` and *not*
    the ``1 - d`` that a cosine-metric table would want. Voyage returns normalised
    vectors, which is what makes the identity hold at all. Getting this backwards
    flags everything or nothing, silently, which is why it is one named function
    with a test on it rather than an expression inside the lookup.
    """
    return 1.0 - (distance * distance) / 2.0


def is_near_duplicate(cosine: float | None, title_score: float, *, threshold: float) -> bool:
    """Spec §4.5's rule: a close vector **and** a title trigram ≥ 0.8.

    ``cosine is None`` means no embedder could produce a vector, and then the
    trigram test runs alone — it still only ever *flags*, never merges.
    """
    if title_score < TITLE_TRIGRAM_MIN:
        return False
    return cosine is None or cosine >= threshold


async def near_duplicate(
    store: KnowledgeStore,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    first_body_vector: Sequence[float],
    title: str,
    threshold: float,
    entry_id: int,
) -> DuplicateMatch | None:
    """The older entry *entry_id* is probably a re-run of, or ``None``.

    The vector is the **first body chunk's**, which is the only one that exists at
    capture time — the summary chunk does not exist until a compile. An empty
    vector is the no-embedder case and runs the trigram leg on its own.

    Only entries **older** than *entry_id* are candidates, which is what makes the
    flag point backwards at the copy that was already there (spec §4.5). A bulk
    run flags after capturing its whole selection, so without that rule the first
    article of a run would be flagged against the last one of the same run and the
    two would point at each other.
    """
    if first_body_vector:
        return await _nearest_by_vector(
            store,
            session_factory,
            vector=first_body_vector,
            title=title,
            threshold=threshold,
            entry_id=entry_id,
        )
    return await _nearest_by_title(
        session_factory, title=title, threshold=threshold, entry_id=entry_id
    )


async def _nearest_by_vector(
    store: KnowledgeStore,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    vector: Sequence[float],
    title: str,
    threshold: float,
    entry_id: int,
) -> DuplicateMatch | None:
    """The nearest older body chunk whose entry's title agrees as well.

    ``k`` is one more than :data:`NEAR_DUPLICATE_K` because the entry being
    captured is its own nearest neighbour and the ``id`` filter below drops it.
    ``include_model_authored`` is set: this is the user's own knowledge base
    checking itself for duplicates, not the authorship gate that governs what the
    model is fed.
    """
    rows = await store.knn(
        list(vector),
        NEAR_DUPLICATE_K + 1,
        filters=SearchFilters(chunk_kinds=("body",), include_model_authored=True),
    )
    chunk_ids = [chunk_id for chunk_id, _ in rows]
    if not chunk_ids:
        return None
    async with session_factory() as session:
        found = (
            await session.execute(
                select(KbChunk.id, KbEntry.id, KbEntry.title)
                .join(KbEntry, KbEntry.id == KbChunk.entry_id)
                .where(
                    KbChunk.id.in_(chunk_ids),
                    KbEntry.deleted_at.is_(None),
                    KbEntry.id < entry_id,
                )
            )
        ).all()
    by_chunk = {chunk_id: (found_id, other) for chunk_id, found_id, other in found}

    for chunk_id, distance in rows:
        candidate = by_chunk.get(chunk_id)
        if candidate is None:
            continue
        cosine = cosine_from_distance(distance)
        score = title_similarity(title, candidate[1])
        if is_near_duplicate(cosine, score, threshold=threshold):
            return DuplicateMatch(entry_id=candidate[0], cosine=cosine, title_score=score)
    return None


async def _nearest_by_title(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    title: str,
    threshold: float,
    entry_id: int,
) -> DuplicateMatch | None:
    """The best-matching title among the older entries — the no-embedder leg.

    Walked newest-first and compared with ``>=`` so that a tie resolves to the
    **oldest** entry: the flag points backwards, at the copy that was already
    there.
    """
    async with session_factory() as session:
        candidates = (
            await session.execute(
                select(KbEntry.id, KbEntry.title)
                .where(KbEntry.deleted_at.is_(None), KbEntry.id < entry_id)
                .order_by(KbEntry.id.desc())
                .limit(NEAR_DUPLICATE_TITLE_SCAN)
            )
        ).all()

    best: DuplicateMatch | None = None
    for candidate_id, other in candidates:
        score = title_similarity(title, other)
        if not is_near_duplicate(None, score, threshold=threshold):
            continue
        if best is None or score >= best.title_score:
            best = DuplicateMatch(entry_id=candidate_id, cosine=None, title_score=score)
    return best


async def flag_near_duplicate(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    entry_id: int,
    *,
    threshold: float = DEFAULT_DUPLICATE_THRESHOLD,
    trigger: str = "manual",
) -> int | None:
    """Set ``possible_duplicate_of`` on *entry_id* if it looks like an older entry.

    **Flag, never merge** — merging is a button the user presses, because the two
    legs together are a heuristic and the one thing a heuristic must not do is
    destroy the evidence it was wrong about.

    Called after the entry has committed and after whatever was going to embed it
    has run, so like every other post-commit consequence its failure is a row in
    the trail rather than a failed capture (the module docstring's rule, spec §5).
    """
    try:
        store = SqliteKnowledgeStore(session_factory)
        async with session_factory() as session:
            entry = await session.get(KbEntry, entry_id)
            if entry is None or entry.deleted_at is not None:
                return None
            title = entry.title

        vector: list[float] = []
        if embedder.dimensions > 0:
            vector = await _first_body_vector(session_factory, store, entry_id)
            if not vector:
                # An embedder is configured but this entry has no vector: the
                # embed did not reach it (Voyage was down, or a bulk run was
                # cancelled). There is nothing to compare, and falling back to the
                # title alone here would be a different rule than the spec's.
                return None

        found = await near_duplicate(
            store,
            session_factory,
            first_body_vector=vector,
            title=title,
            threshold=threshold,
            entry_id=entry_id,
        )
        if found is None:
            return None

        async with session_factory() as session:
            entry = await session.get(KbEntry, entry_id)
            if entry is None:
                return None
            entry.possible_duplicate_of = found.entry_id
            cosine = "n/a" if found.cosine is None else f"{found.cosine:.3f}"
            await log_activity(
                session,
                "capture",
                entry_id=entry_id,
                source=trigger,
                detail=(
                    f"possible duplicate of entry {found.entry_id}"
                    f" (cosine {cosine}, title {found.title_score:.2f})"
                ),
            )
            await session.commit()
        return found.entry_id
    except Exception as exc:  # noqa: BLE001 - the capture already committed
        logger.exception("The near-duplicate check for entry %s failed", entry_id)
        await _log_in_new_session(
            session_factory,
            "skip",
            entry_id=entry_id,
            source=trigger,
            detail=f"near-duplicate check failed: {type(exc).__name__}: {exc}",
        )
        return None


async def _first_body_vector(
    session_factory: async_sessionmaker[AsyncSession],
    store: KnowledgeStore,
    entry_id: int,
) -> list[float]:
    """The stored vector of the entry's lowest-``ord`` body chunk, or ``[]``.

    Read back out of ``kb_chunk_vec`` rather than re-embedded: the text was
    embedded moments ago, and a second Voyage call per capture would double the
    bill of every bulk run to buy a number the database already holds.
    """
    chunk = (await store.first_body_chunks([entry_id])).get(entry_id)
    if chunk is None:
        return []
    async with session_factory() as session:
        raw = await session.scalar(
            text("SELECT vec_to_json(embedding) FROM kb_chunk_vec WHERE chunk_id = :chunk_id"),
            {"chunk_id": chunk.id},
        )
    return list(json.loads(raw)) if raw else []


# ----------------------------------------------------------------- internals


async def _find_duplicate(
    session: AsyncSession,
    *,
    url: str | None,
    feed_item_id: int | None,
    note_id: int | None,
    digest: str,
) -> KbEntry | None:
    """The entry this capture is already, if any — in dedup-key order.

    The key is the canonical URL, else the feed item id, else the content hash
    (spec §4.5). The hash is the **fallback**, not an extra check: two different
    URLs carrying the same syndicated text are two articles, and flagging them as
    one belongs to the near-duplicate pass, which can be undone.

    ``feed_item_id`` and ``note_id`` are consulted first regardless, because their
    uniqueness indexes have no ``deleted_at`` scope — a second entry could not take
    that id even if this function wanted one. The URL and content-hash lookups do
    skip soft-deleted entries, because their index *is* scoped that way and
    re-capturing something the user deleted is allowed.

    A capture that *named* a source and found nothing is a **new** entry, not a
    hash lookup: the hash would hand a note the entry of an article with the same
    body, and that entry has no ``note_id``, so the next save of the note would
    fall through again and the note could never acquire an entry of its own.
    """
    if feed_item_id is not None:
        found = await session.scalar(select(KbEntry).where(KbEntry.feed_item_id == feed_item_id))
        if found is not None:
            return found
    if note_id is not None:
        found = await session.scalar(select(KbEntry).where(KbEntry.note_id == note_id))
        if found is not None:
            return found
    if url:
        return await session.scalar(
            select(KbEntry).where(KbEntry.url == url, KbEntry.deleted_at.is_(None))
        )
    if feed_item_id is not None or note_id is not None:
        return None
    return await session.scalar(
        select(KbEntry)
        .where(KbEntry.content_hash == digest, KbEntry.deleted_at.is_(None))
        .order_by(KbEntry.id)
    )


async def _add_backlink(
    session: AsyncSession,
    entry_id: int,
    *,
    feed_item_id: int | None,
    note_id: int | None,
    session_id: int | None,
) -> bool:
    """Record where this entry was used, unless the same link is already there."""
    if feed_item_id is None and note_id is None and session_id is None:
        return False
    found = await session.scalar(
        select(KbEntryLink.id).where(
            KbEntryLink.entry_id == entry_id,
            KbEntryLink.feed_item_id.is_(feed_item_id)
            if feed_item_id is None
            else KbEntryLink.feed_item_id == feed_item_id,
            KbEntryLink.note_id.is_(note_id) if note_id is None else KbEntryLink.note_id == note_id,
            KbEntryLink.session_id.is_(session_id)
            if session_id is None
            else KbEntryLink.session_id == session_id,
        )
    )
    if found is not None:
        return False
    session.add(
        KbEntryLink(
            entry_id=entry_id,
            feed_item_id=feed_item_id,
            note_id=note_id,
            session_id=session_id,
        )
    )
    return True


async def _add_snapshot(
    session: AsyncSession, entry: KbEntry, body: str, *, version: int
) -> KbSnapshot:
    snapshot = KbSnapshot(
        entry_id=entry.id,
        version=version,
        text=body,
        sha256=snapshot_hash(body),
        chars=len(body),
    )
    session.add(snapshot)
    await session.flush()
    return snapshot


async def _current_version(session: AsyncSession, entry_id: int) -> int:
    return (
        await session.scalar(
            select(func.coalesce(func.max(KbSnapshot.version), 0)).where(
                KbSnapshot.entry_id == entry_id
            )
        )
        or 0
    )


async def _rechunk(
    session: AsyncSession, entry: KbEntry, snapshot: KbSnapshot, body: str
) -> list[KbChunk]:
    """Replace the entry's body chunks with the ones *body* splits into.

    The delete is what takes the old FTS rows and vectors with it — both are
    maintained by ``AFTER DELETE`` triggers on ``kb_chunks``, which a bulk SQL
    delete fires exactly as a row-by-row one would.
    """
    await session.execute(
        delete(KbChunk).where(KbChunk.entry_id == entry.id, KbChunk.kind == "body")
    )
    chunks = [
        KbChunk(
            entry_id=entry.id,
            snapshot_id=snapshot.id,
            ord=piece.ord,
            text=piece.text,
            token_estimate=piece.token_estimate,
            kind="body",
        )
        for piece in split_markdown(body)
    ]
    session.add_all(chunks)
    await session.flush()
    return chunks


async def _sync_regex_entities(session: AsyncSession, entry_id: int, text: str) -> None:
    """Re-derive the regex entities, leaving model- and user-sourced rows alone."""
    await session.execute(
        delete(KbEntryEntity).where(
            KbEntryEntity.entry_id == entry_id, KbEntryEntity.source == "regex"
        )
    )
    held = set(
        (
            await session.execute(
                select(KbEntryEntity.kind, KbEntryEntity.value).where(
                    KbEntryEntity.entry_id == entry_id
                )
            )
        ).all()
    )
    for kind, value in extract_entities(text):
        if (kind, value) in held:
            continue
        session.add(KbEntryEntity(entry_id=entry_id, kind=kind, value=value, source="regex"))
        held.add((kind, value))


async def _log_in_new_session(
    session_factory: async_sessionmaker[AsyncSession], action: str, **fields: object
) -> None:
    async with session_factory() as session:
        await log_activity(session, action, **fields)  # type: ignore[arg-type]
        await session.commit()


def entry_ids(rows: Iterable[KbEntry]) -> list[int]:
    """The ids of *rows*, in order. Small, but every caller needs it."""
    return [row.id for row in rows]


__all__ = [
    "ACTIVITY_MAX_ROWS",
    "DEFAULT_DUPLICATE_THRESHOLD",
    "DEFAULT_MIN_SNAPSHOT_CHARS",
    "NEAR_DUPLICATE_K",
    "NEAR_DUPLICATE_TITLE_SCAN",
    "NOTE_SEPARATOR",
    "TITLE_TRIGRAM_MIN",
    "CaptureResult",
    "DuplicateMatch",
    "KbConflict",
    "RefreshResult",
    "capture_article",
    "capture_note",
    "capture_url",
    "content_hash",
    "cosine_from_distance",
    "embed_pending",
    "entry_ids",
    "flag_near_duplicate",
    "is_near_duplicate",
    "log_activity",
    "merge_entries",
    "near_duplicate",
    "normalise_text",
    "purge",
    "refresh_snapshot",
    "snapshot_hash",
    "soft_delete",
    "title_similarity",
    "trigrams",
    "undelete",
]
