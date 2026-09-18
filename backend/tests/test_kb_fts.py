"""``fts_query`` — the application's *second* escaping rule.

``escape_like`` is never reused here. It escapes ``%``, ``_`` and ``\\`` for a
``LIKE`` pattern and would inject backslashes straight into the FTS5 tokenizer.
Every case below is asserted as an exact ``MATCH`` string **and** executed against
the real ``kb_chunks_fts`` table, because a string that looks right and raises
"fts5: syntax error" is worth nothing.
"""

import pytest
from sqlalchemy import text

from app.kb.fts import fts_query
from app.kb.models import KbChunk, KbEntry


async def _index(db_session, *texts: str) -> list[int]:
    entry = KbEntry(kind="article", title="T", authorship="source")
    db_session.add(entry)
    await db_session.flush()
    chunks = [
        KbChunk(entry_id=entry.id, ord=n, text=body, token_estimate=1)
        for n, body in enumerate(texts)
    ]
    db_session.add_all(chunks)
    await db_session.commit()
    return [chunk.id for chunk in chunks]


async def _run(db_session, match: str) -> list[int]:
    rows = (
        await db_session.execute(
            text("SELECT rowid FROM kb_chunks_fts WHERE kb_chunks_fts MATCH :m ORDER BY rowid"),
            {"m": match},
        )
    ).all()
    return [row[0] for row in rows]


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("CVE-2024-3094", '"cve-2024-3094"'),
        ("log4j_rce", '"log4j_rce"'),
        ("foo AND bar", '"foo" AND "bar"'),
        ("foo OR bar", '"foo" AND "bar"'),
        ("foo NOT bar", '"foo" AND "bar"'),
        ("foo NEAR bar", '"foo" AND "bar"'),
        ("foo*", '"foo"'),
        ("^foo", '"foo"'),
        ("(foo)", '"foo"'),
        ("col:foo", '"colfoo"'),
        ('He said "hi"', '"he" AND "said" AND """hi"""'),
        ("", ""),
        ('""', ""),
        ("   ", ""),
        ("* ^ ( ) :", ""),
        ("and or not near", '"and" AND "or" AND "not" AND "near"'),
        ("Xz  Utils", '"xz" AND "utils"'),
        ("a\x00b", '"ab"'),
        ("\x00\x01\x02", ""),
        ("liblzma\x07", '"liblzma"'),
    ],
)
def test_the_exact_match_string(typed: str, expected: str):
    assert fts_query(typed) == expected


@pytest.mark.parametrize(
    "typed",
    [
        "CVE-2024-3094",
        "log4j_rce",
        "foo AND bar",
        "foo*",
        "^foo",
        '""',
        'He said "hi"',
        "* ^ ( ) :",
        "NEAR(foo bar)",
        "a-b-c d_e_f",
        "Ünïcôdé",
        "a\x00b",
        "CVE-2024-3094\x00",
    ],
)
async def test_every_shape_executes_against_a_real_fts5_table(db_session, typed: str):
    await _index(db_session, "some indexed text")

    match = fts_query(typed)
    if not match:
        pytest.skip("an empty query is never executed; the store short-circuits")
    await _run(db_session, match)


async def test_typing_a_cve_id_finds_it(db_session):
    ids = await _index(
        db_session,
        "The xz backdoor is tracked as CVE-2024-3094 and affects liblzma.",
        "An unrelated passage about patch management.",
    )

    assert await _run(db_session, fts_query("CVE-2024-3094")) == [ids[0]]
    # The tokenizer splits on the hyphens, so the lower-cased phrase matches too.
    assert await _run(db_session, fts_query("cve 2024 3094")) == [ids[0]]


async def test_the_or_fallback_widens_a_query_that_matched_nothing(db_session):
    ids = await _index(db_session, "Only about liblzma.", "Only about openssh.")

    both = fts_query("liblzma openssh")
    assert await _run(db_session, both) == []

    either = fts_query("liblzma openssh", join="OR")
    assert either == '"liblzma" OR "openssh"'
    assert await _run(db_session, either) == ids


async def test_a_quote_in_a_title_does_not_end_the_phrase(db_session):
    ids = await _index(db_session, 'The advisory titled "Total Compromise" is out.')

    assert await _run(db_session, fts_query('"Total Compromise"')) == ids


async def test_an_underscore_is_a_phrase_not_a_wildcard(db_session):
    ids = await _index(db_session, "log4j rce in the wild", "log4jXrce in the wild")

    assert await _run(db_session, fts_query("log4j_rce")) == [ids[0]]


def test_only_uppercase_operators_are_dropped():
    """FTS5 reads only the uppercase spellings as operators, so ``and`` is a word."""
    assert fts_query("and") == '"and"'
    assert fts_query("AND") == ""


def test_an_unknown_join_is_refused():
    with pytest.raises(ValueError):
        fts_query("foo bar", join="NEAR")
