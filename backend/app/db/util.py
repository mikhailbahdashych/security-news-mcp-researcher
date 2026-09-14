"""Small SQL helpers shared by every query module."""

from __future__ import annotations

from sqlalchemy import ColumnElement

#: The character passed to ``LIKE ... ESCAPE``. Backslash is the conventional choice
#: and the one :func:`escape_like` escapes first.
LIKE_ESCAPE_CHAR = "\\"


def escape_like(value: str) -> str:
    """Escape a user string so it matches literally inside a ``LIKE`` pattern.

    ``%`` and ``_`` are SQL wildcards, so a search for ``100%`` or ``log4j_rce``
    would otherwise match far more than the user asked for. The escape character
    itself has to be escaped first, or ``\\%`` typed by the user would come out as
    an escaped-percent instead of a literal backslash followed by a wildcard.

    The result is only valid when the query uses the matching escape clause, so
    callers should reach for :func:`matches` rather than assembling one by hand.

    This is the single escaping rule for the whole application — the inbox, notes
    and global search all reuse it rather than inlining a second one.
    """
    return (
        value.replace(LIKE_ESCAPE_CHAR, LIKE_ESCAPE_CHAR * 2)
        .replace("%", f"{LIKE_ESCAPE_CHAR}%")
        .replace("_", f"{LIKE_ESCAPE_CHAR}_")
    )


def like_pattern(value: str) -> str:
    """The ``%value%`` pattern for a substring match, wildcards escaped."""
    return f"%{escape_like(value)}%"


def matches(column: ColumnElement[str | None], value: str) -> ColumnElement[bool]:
    """``column LIKE '%value%'`` — the single substring-match rule for the app.

    Plain ``LIKE`` rather than ``ilike()``: SQLite's ``LIKE`` is already
    case-insensitive for ASCII (the ``case_sensitive_like`` pragma is off), which
    is exactly what CVE ids, vendor names and English prose need, while
    ``ilike()`` would wrap both sides in ``lower()`` for no gain — SQLite's
    ``lower()`` is ASCII-only as well. Non-ASCII text is therefore matched
    case-sensitively; that is an accepted limitation of SQLite's default
    collation, not something a helper here can paper over.
    """
    return column.like(like_pattern(value), escape=LIKE_ESCAPE_CHAR)


__all__ = ["LIKE_ESCAPE_CHAR", "escape_like", "like_pattern", "matches"]
