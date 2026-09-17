"""Entities pulled out of an entry's text at capture time.

Only CVE ids for now, and only by regex (``source='regex'``). Vendors and products
arrive later from a compile's suggestions (``source='model'``) or from the user
(``source='user'``); re-running this over stored snapshots is cheap, so the set can
grow without a migration.

Exact ids are what makes "have we covered CVE-2024-3094?" answerable exactly,
ahead of both search legs, rather than approximately.
"""

from __future__ import annotations

import re

#: ``CVE-YYYY-NNNN`` with **four or more** digits in the sequence — five- and
#: six-digit ids are real (``CVE-2014-100001``). Case-insensitive because prose
#: and URLs both lower-case it; the stored value is always upper-cased.
CVE_PATTERN = re.compile(r"CVE-\d{4}-\d{4,}", re.IGNORECASE)

EntityKind = str


def extract_entities(text: str) -> list[tuple[EntityKind, str]]:
    """``[(kind, value)]`` in order of first appearance, de-duplicated.

    Order matters only for readability — the table's primary key is
    ``(entry_id, kind, value)`` — but a stable order keeps the tests honest.
    """
    seen: set[tuple[str, str]] = set()
    found: list[tuple[str, str]] = []
    for match in CVE_PATTERN.finditer(text or ""):
        entity = ("cve", match.group(0).upper())
        if entity not in seen:
            seen.add(entity)
            found.append(entity)
    return found


__all__ = ["CVE_PATTERN", "extract_entities"]
