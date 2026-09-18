"""Request bodies for the bulk-capture job (Task 2.3).

Its own module rather than more models in ``app/schemas/kb.py``, which belongs to
the compile task this phase (plan decision P2-2). The *responses* are SSE frames,
not models — the shapes are in
``docs/superpowers/specs/2026-09-17-knowledge-base-api-phase2.md`` §3 and in
``app/kb/bulk.py`` — except the cancel, which reuses
``app.schemas.common.CancelResponse``.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

#: The most items one job will take. Two hundred articles is already minutes of
#: fetching; beyond it the answer is two clicks, not a longer stream.
MAX_BULK_ITEMS = 200

#: A job id is a client-generated opaque handle (a ``uuid4().hex``), never looked
#: up in the database — it only ever names a key in the task registry.
MAX_JOB_ID_CHARS = 100


class BulkCaptureRequest(BaseModel):
    """``POST /api/kb/bulk`` — save these inbox items to the knowledge base.

    *job_id* is optional and the server generates one when it is absent, but a
    client that wants a Stop button should send its own: cancelling needs the id
    before the first frame arrives, and a stream is not a way to get one in time.
    """

    item_ids: Annotated[list[int], Field(min_length=1, max_length=MAX_BULK_ITEMS)]
    job_id: Annotated[str | None, Field(max_length=MAX_JOB_ID_CHARS)] = None


class BulkCancelRequest(BaseModel):
    """``POST /api/kb/bulk/cancel`` — stop the run under this id."""

    job_id: Annotated[str, Field(min_length=1, max_length=MAX_JOB_ID_CHARS)]


__all__ = [
    "MAX_BULK_ITEMS",
    "MAX_JOB_ID_CHARS",
    "BulkCancelRequest",
    "BulkCaptureRequest",
]
