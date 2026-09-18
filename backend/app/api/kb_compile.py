"""Compile and the monthly budget (Task 2.5).

``POST /kb/entries/{id}/compile``, ``POST /kb/compile`` and ``GET /kb/budget``.

Its own module, not more routes in ``app/api/kb.py``, so the two Phase 2 tasks that add
knowledge-base routes never share a file (plan decision P2-2).
"""

from fastapi import APIRouter

router = APIRouter(prefix="/kb", tags=["kb"])
