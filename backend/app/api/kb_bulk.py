"""Bulk capture into the knowledge base (Task 2.3): ``POST /kb/bulk`` and its cancel.

Its own module, not more routes in ``app/api/kb.py``, so the two Phase 2 tasks that add
knowledge-base routes never share a file (plan decision P2-2).

The stream is note generation's shape exactly: ``pump_agent_events`` owns the
cancellable task, the runner's events are forwarded verbatim, and the **terminal
``done`` frame is this module's**, emitted after the pump has finished. That is
what lets it say "here is what is in the database now" — including after a
cancel, which is the whole of "the page shows what was saved".
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response, status
from sse_starlette.sse import EventSourceResponse

from app.agent import events as ev
from app.api import tasks as task_registry
from app.api.deps import KbServiceDep
from app.api.streaming import (
    ALREADY_RUNNING_MESSAGE,
    SSE_HEADERS,
    SSE_PING_S,
    pump_agent_events,
    sse_data,
    sse_frame,
)
from app.kb.bulk import BulkOutcome, bulk_key, run_bulk_capture
from app.kb.service import KbService
from app.schemas.common import CancelResponse
from app.schemas.kb_bulk import BulkCancelRequest, BulkCaptureRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/kb", tags=["kb"])


@router.post("/bulk")
async def bulk_capture(payload: BulkCaptureRequest, request: Request, kb: KbServiceDep) -> Response:
    """Save many inbox items to the knowledge base, streamed.

    The 409 is the fast path for a second click; the race it cannot close is
    covered by ``pump_agent_events``, which answers a lost registration with one
    ``error`` frame and ends the stream.

    ``kb`` is held across the stream for the reason ``api/notes.py`` spells out:
    ``get_kb_service`` does not *yield*, so nothing about it is torn down when the
    route returns and the body starts. The threshold is resolved **here**, before
    the stream opens, for the same reason — it is a database read, and a
    yield-dependency's session is finalised before a streamed body is sent.
    """
    job_id = payload.job_id or uuid4().hex
    key = bulk_key(job_id)
    if await task_registry.is_running(key):
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="That bulk capture is already running."
        )

    threshold = await kb.duplicate_threshold()
    return EventSourceResponse(
        _stream_bulk(request, kb, payload, job_id, key, threshold),
        ping=SSE_PING_S,
        headers=SSE_HEADERS,
    )


async def _stream_bulk(
    request: Request,
    kb: KbService,
    payload: BulkCaptureRequest,
    job_id: str,
    key: str,
    threshold: float,
) -> AsyncIterator[dict[str, str]]:
    """Forward the job's events, then say what landed.

    ``client=None``: there is no Anthropic call anywhere in this job, so there is
    no client for the pump to close.
    """
    outcome = BulkOutcome()
    generator = run_bulk_capture(
        kb,
        payload.item_ids,
        outcome=outcome,
        duplicate_threshold=threshold,
        transport=kb.transport,
    )

    already_running = False
    async for event in pump_agent_events(request, generator, key=key, client=None):
        if isinstance(event, ev.Error) and event.message == ALREADY_RUNNING_MESSAGE:
            # A duplicate key lost the race. Nothing ran, so there is nothing to
            # report done about; the frame the pump produced is the whole answer.
            already_running = True
        if isinstance(event, ev.TurnStart):
            # The client may not have supplied an id, and it needs one to be able
            # to press Stop.
            name, sse_payload = event.to_sse()
            yield sse_data(name, {**sse_payload, "job_id": job_id})
            continue
        yield sse_frame(event)

    if already_running:
        return
    logger.info(
        "Bulk capture %s saved %d and skipped %d of %d items",
        job_id,
        outcome.saved,
        outcome.skipped,
        outcome.total,
    )
    yield sse_data("done", outcome.done_payload())


@router.post("/bulk/cancel", response_model=CancelResponse)
async def cancel_bulk_capture(payload: BulkCancelRequest) -> CancelResponse:
    """Stop an in-flight bulk run. Nothing running is a 200 with ``false`` — the
    Stop button is allowed to lose the race, and what was captured stays."""
    cancelled = await task_registry.cancel(bulk_key(payload.job_id))
    return CancelResponse(cancelled=cancelled)


__all__ = ["bulk_key", "router"]
