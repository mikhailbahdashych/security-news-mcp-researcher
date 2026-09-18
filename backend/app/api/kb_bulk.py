"""Bulk capture into the knowledge base (Task 2.3): ``POST /kb/bulk`` and its cancel.

Its own module, not more routes in ``app/api/kb.py``, so the two Phase 2 tasks that add
knowledge-base routes never share a file (plan decision P2-2).
"""

from fastapi import APIRouter

router = APIRouter(prefix="/kb", tags=["kb"])
