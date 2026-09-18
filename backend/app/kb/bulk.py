"""Saving many inbox items to the knowledge base as one cancellable job.

Forty articles through ``fetch_guarded`` is minutes of work, so this is a **job**
and not a loop inside a request (spec §4.5): the route streams it on
``pump_agent_events`` under the task-registry key ``kb:bulk:{job_id}``, exactly
like note generation, and Stop cancels the task rather than merely closing a
stream nobody is reading.

Three ordering rules make it a job rather than a fan-out:

* **Eight extractions at a time**, no more (:data:`MAX_KB_EXTRACTIONS`). The cap
  is about the *sites* being read, not about this process.
* **One embed for the whole run, not one per article.** ``embed_pending`` writes
  one ``kb_activity`` row per call, so embedding each entry as it lands would put
  two hundred rows in the trail for one click and send two hundred Voyage
  requests where the batcher would have sent a handful. Every capture therefore
  runs with ``defer_embedding=True`` and the run embeds once at the end.
* **The near-duplicate pass comes after that embed**, because it reads the vector
  the embed wrote. Its cost is one KNN per new entry and no outbound call at all.

**A cancelled run leaves its chunks pending.** Neither the embed nor the
duplicate pass runs, because both are the last thing the job does and Stop has to
be instant — a Stop that first waited out a Voyage round trip for two hundred
articles is not a Stop. Pending chunks are the knowledge base's ordinary resting
state: ``embedded_at IS NULL`` is exactly what Settings → Knowledge → **Embed
now** resumes from. What was captured stays captured.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

import httpx2

from app.agent import events as ev
from app.kb.capture import (
    DEFAULT_DUPLICATE_THRESHOLD,
    embed_pending,
    flag_near_duplicate,
)
from app.kb.service import KbService

logger = logging.getLogger(__name__)

#: How many articles are fetched and extracted at the same time. The ceiling is
#: the *sites*': eight parallel reads is neighbourly, eighty is a scrape.
MAX_KB_EXTRACTIONS = 8

#: How many pending chunks the end-of-run embed takes in one go. Generous, because
#: it is meant to cover the whole run — the batcher splits it into properly sized
#: Voyage requests anyway, and this is one selection query and one activity row.
#: Anything left over stays pending for **Embed now**, which is where a backlog
#: belongs.
BULK_EMBED_LIMIT = 5_000

#: What ``kb_activity.source`` and every capture trigger of this job are called.
BULK_SOURCE = "bulk"


def bulk_key(job_id: str) -> str:
    """The cancellation-registry key for one bulk run."""
    return f"kb:bulk:{job_id}"


@dataclass(slots=True)
class BulkOutcome:
    """What the run committed, read by the route after the pump finishes.

    A mutable object handed in rather than a return value because the job is an
    async generator: the route needs these numbers to write the terminal ``done``
    frame, including after a cancel, which is the whole point of "the page shows
    what was saved". The same shape as ``notes_service.SourceCollector``'s role in
    the generation stream, and for the same reason.
    """

    total: int = 0
    entry_ids: list[int] = field(default_factory=list)
    saved: int = 0
    skipped: int = 0
    duplicates: int = 0

    def done_payload(self) -> dict[str, object]:
        """The terminal frame's data, as the HTTP contract spells it."""
        return {
            "saved": self.saved,
            "skipped": self.skipped,
            "duplicates": self.duplicates,
            "entry_ids": list(self.entry_ids),
        }


@dataclass(frozen=True, slots=True)
class _ItemResult:
    """One item's outcome, on its way to one ``text_delta`` frame."""

    item_id: int
    entry_id: int | None = None
    created: bool = False
    skipped_reason: str | None = None
    possible_duplicate_of: int | None = None

    def payload(self, *, done: int, total: int) -> dict[str, object]:
        return {
            "item_id": self.item_id,
            "entry_id": self.entry_id,
            "created": self.created,
            "skipped_reason": self.skipped_reason,
            "possible_duplicate_of": self.possible_duplicate_of,
            "done": done,
            "total": total,
        }


async def run_bulk_capture(
    kb: KbService,
    item_ids: Sequence[int],
    *,
    outcome: BulkOutcome,
    duplicate_threshold: float = DEFAULT_DUPLICATE_THRESHOLD,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> AsyncIterator[ev.AgentEvent]:
    """Capture every item in *item_ids*, yielding one event per finished item.

    Results come out **in completion order, not submission order**: a paywalled
    site that takes fifteen seconds must not hold back the seven that answered
    immediately, and the client has the ``item_id`` in every frame.

    The event vocabulary is the agent package's (``app/agent/events.py``) and
    Phase 2 does not widen it, so each item's payload rides as JSON inside a
    ``TextDelta``. The terminal ``done`` frame is the **route's**, emitted after
    the pump finishes — like note generation, and for the same reason: it means
    "this is what is in the database now", which only the code outside the
    cancellable task can honestly say.
    """
    wanted = list(dict.fromkeys(int(item_id) for item_id in item_ids))
    outcome.total = len(wanted)
    yield ev.TurnStart(turn=0)

    limit = asyncio.Semaphore(MAX_KB_EXTRACTIONS)

    created: list[int] = []

    async def capture_one(item_id: int) -> _ItemResult:
        async with limit:
            # ``guarded`` turns one paywall, one 403 or one unknown id into a row
            # in the trail and a ``None`` here, so a single bad item cannot end a
            # run of two hundred.
            result = await kb.guarded(
                kb.capture_feed_item(
                    item_id,
                    captured_by="user",
                    trigger=BULK_SOURCE,
                    transport=transport,
                    defer_embedding=True,
                ),
                source=BULK_SOURCE,
            )
        if result is None:
            outcome.skipped += 1
            return _ItemResult(item_id=item_id, skipped_reason="the capture failed")
        # Counted **here**, not where the frame is emitted: the terminal ``done``
        # has to name what is in the database, and a Stop pressed between this
        # commit and the loop's next turn must not lose the entry it already has.
        if result.entry_id is None:
            outcome.skipped += 1
        else:
            outcome.saved += 1
            outcome.entry_ids.append(result.entry_id)
            if result.created:
                created.append(result.entry_id)
            if result.possible_duplicate_of is not None:
                outcome.duplicates += 1
        return _ItemResult(
            item_id=item_id,
            entry_id=result.entry_id,
            created=result.created,
            skipped_reason=result.skipped_reason,
            possible_duplicate_of=result.possible_duplicate_of,
        )

    tasks = [asyncio.create_task(capture_one(item_id)) for item_id in wanted]
    try:
        done = 0
        for finished in asyncio.as_completed(tasks):
            result = await finished
            done += 1
            yield ev.TextDelta(text=json.dumps(result.payload(done=done, total=len(wanted))))
    finally:
        # A cancel arrives as CancelledError at the await above, and the extractions
        # still in flight have to go with it. Not awaited: whatever they committed
        # is committed, and Stop must not wait on an outbound fetch.
        for task in tasks:
            if not task.done():
                task.cancel()

    outcome.duplicates += await _embed_and_flag(
        kb, created, duplicate_threshold=duplicate_threshold
    )


async def _embed_and_flag(
    kb: KbService, entry_ids: Sequence[int], *, duplicate_threshold: float
) -> int:
    """Embed the run's backlog once, then flag near-duplicates. Returns the flags.

    Both halves are best-effort by design (spec §5): an embedding provider being
    down leaves the chunks pending and the entries keyword-searchable, which is a
    normal state of the knowledge base and not a reason to fail a save the user
    already watched succeed. ``flag_near_duplicate`` has the same contract
    built in.
    """
    if not entry_ids:
        return 0
    try:
        await embed_pending(kb.session_factory, kb.embedder, limit=BULK_EMBED_LIMIT, source="bulk")
    except Exception:  # noqa: BLE001 - the entries are committed; chunks stay pending
        logger.exception("The bulk run's embedding failed; its chunks stay pending")

    flagged = 0
    # In creation order, so an entry can be flagged against one captured earlier
    # in the same run — which is exactly what saving a page of near-identical
    # headlines produces.
    for entry_id in entry_ids:
        found = await flag_near_duplicate(
            kb.session_factory,
            kb.embedder,
            entry_id,
            threshold=duplicate_threshold,
            trigger=BULK_SOURCE,
        )
        if found is not None:
            flagged += 1
    return flagged


__all__ = [
    "BULK_EMBED_LIMIT",
    "BULK_SOURCE",
    "MAX_KB_EXTRACTIONS",
    "BulkOutcome",
    "bulk_key",
    "run_bulk_capture",
]
