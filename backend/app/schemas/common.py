"""Schemas shared by more than one domain.

Only what genuinely has one meaning across routes belongs here — a domain model
that happens to look like another's is not the same model.
"""

from __future__ import annotations

from pydantic import BaseModel


class CancelResponse(BaseModel):
    """The answer to "stop that": whether anything was actually running.

    Shared by ``POST /api/sessions/{id}/cancel`` and
    ``POST /api/notes/generate/cancel``, which mean the same thing by it — the
    Stop button is allowed to lose the race, so nothing running is a 200 with
    ``false`` rather than a 404.
    """

    cancelled: bool


__all__ = ["CancelResponse"]
