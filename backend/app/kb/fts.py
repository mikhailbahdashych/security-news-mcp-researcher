"""Building an FTS5 ``MATCH`` string out of what a human typed.

This is the application's **second** escaping rule, and it exists because the
first one cannot be reused. ``app.db.util.escape_like`` escapes ``%``, ``_`` and
``\\`` so a string matches literally inside a ``LIKE`` pattern; FTS5 has no
wildcards of that shape, and those backslashes would go straight into the
tokenizer as text. ``LIKE`` searches use ``matches``/``escape_like``; FTS5
searches use :func:`fts_query`. Nothing uses both.

The rule, in full:

* split the typed text on whitespace;
* strip the stray operator punctuation FTS5 would read as syntax (``^ * : ( )``);
* drop a term that is a bare operator — FTS5 only reads the **uppercase**
  ``AND`` / ``OR`` / ``NOT`` / ``NEAR`` that way, so a lower-case ``and`` is a word;
* drop a term with no alphanumeric character left, which is what keeps a query of
  pure punctuation from becoming ``""`` and raising a syntax error;
* double any embedded ``"`` and wrap **every** term as a quoted phrase.

Quoting every term is the point of the whole exercise. ``CVE-2024-3094`` becomes
``"cve-2024-3094"``, which the ``unicode61`` tokenizer reads as the adjacent
tokens ``cve`` / ``2024`` / ``3094`` — a phrase query that matches correctly.
Unquoted, the hyphens would be read as syntax.

Terms are joined with ``AND``; a caller that gets nothing back retries with
``OR``. That is deliberately the caller's decision, because only it knows whether
an empty result is worth widening.
"""

from __future__ import annotations

#: Punctuation FTS5 reads as syntax: a prefix ``*``, a column filter ``:``, an
#: initial-token ``^`` and the grouping parentheses. Removed from every term.
OPERATOR_CHARS = "^*:()"

#: C0 control characters, removed for the same reason as the punctuation above: a
#: term containing ``\x00`` produces a phrase SQLite reads as an unterminated
#: string, which is the one thing this module promises never to hand back. They
#: reach us from a JSON request body and, in Phase 3, from model-supplied tool
#: input; ``str.split`` already drops the whitespace ones between terms.
CONTROL_CHARS = "".join(chr(code) for code in range(32))

#: The bare operators. Uppercase only — that is how FTS5 itself reads them.
OPERATORS = frozenset({"AND", "OR", "NOT", "NEAR"})

#: How terms may be joined. ``AND`` is the default; ``OR`` is the fallback a caller
#: reaches for when ``AND`` returned nothing.
JOINS = ("AND", "OR")

_STRIPPER = str.maketrans("", "", OPERATOR_CHARS + CONTROL_CHARS)


def fts_query(user_text: str, *, join: str = "AND") -> str:
    """Turn typed text into an FTS5 ``MATCH`` string, or ``""`` when nothing is left.

    An empty result is never executed — ``MATCH ''`` is a syntax error — so the
    store short-circuits to no rows instead.
    """
    if join not in JOINS:
        raise ValueError(f"join must be one of {JOINS}, got {join!r}")

    terms: list[str] = []
    for raw in (user_text or "").split():
        term = raw.translate(_STRIPPER)
        if term in OPERATORS:
            continue
        if not any(character.isalnum() for character in term):
            continue
        terms.append('"' + term.lower().replace('"', '""') + '"')

    return f" {join} ".join(terms)


__all__ = ["CONTROL_CHARS", "JOINS", "OPERATORS", "OPERATOR_CHARS", "fts_query"]
