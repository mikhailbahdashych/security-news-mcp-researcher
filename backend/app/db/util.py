"""Small SQL helpers shared by every query module."""

from __future__ import annotations

#: The character passed to ``LIKE ... ESCAPE``. Backslash is the conventional choice
#: and the one :func:`escape_like` escapes first.
LIKE_ESCAPE_CHAR = "\\"


def escape_like(value: str) -> str:
    """Escape a user string so it matches literally inside a ``LIKE`` pattern.

    ``%`` and ``_`` are SQL wildcards, so a search for ``100%`` or ``log4j_rce``
    would otherwise match far more than the user asked for. The escape character
    itself has to be escaped first, or ``\\%`` typed by the user would come out as
    an escaped-percent instead of a literal backslash followed by a wildcard.

    The result is only valid when the query uses the matching escape clause::

        column.ilike(f"%{escape_like(q)}%", escape=LIKE_ESCAPE_CHAR)

    This is the single escaping rule for the whole application — notes search and
    global search reuse it rather than inlining a second one.
    """
    return (
        value.replace(LIKE_ESCAPE_CHAR, LIKE_ESCAPE_CHAR * 2)
        .replace("%", f"{LIKE_ESCAPE_CHAR}%")
        .replace("_", f"{LIKE_ESCAPE_CHAR}_")
    )


__all__ = ["LIKE_ESCAPE_CHAR", "escape_like"]
