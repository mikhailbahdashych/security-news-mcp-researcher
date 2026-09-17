"""Markdown chunking — pure, no I/O, no database."""

import math

import pytest

from app.kb.chunking import CHARS_PER_TOKEN, Chunk, estimate_tokens, split_markdown


def _shared_boundary(previous: str, following: str) -> str:
    """The longest suffix of *previous* that *following* starts with."""
    for size in range(min(len(previous), len(following)), 0, -1):
        if previous.endswith(following[:size]):
            return following[:size]
    return ""


def test_the_token_estimate_is_chars_over_three_point_six():
    """Not chars/4. Security prose is dense with hashes, CVE ids, version strings
    and code, and chars/4 under-counts it — the same estimate is what
    ``kb_compile_max_chars`` and the "Compile N" estimate are compared against."""
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1
    assert estimate_tokens("x" * 36) == 10
    assert estimate_tokens("x" * 37) == 11
    for length in (1, 17, 360, 4001):
        assert estimate_tokens("x" * length) == math.ceil(length / 3.6)


@pytest.mark.parametrize("text", ["", "   ", "\n\n\n", "\t \n"])
def test_nothing_in_nothing_out(text: str):
    assert split_markdown(text) == []


def test_short_text_is_one_chunk():
    chunks = split_markdown("A short paragraph about CVE-2024-3094.")

    assert len(chunks) == 1
    assert chunks[0] == Chunk(
        ord=0,
        text="A short paragraph about CVE-2024-3094.",
        token_estimate=estimate_tokens("A short paragraph about CVE-2024-3094."),
    )


def test_paragraphs_are_packed_up_to_the_target():
    paragraph = "word " * 20  # ~100 chars, ~28 tokens
    text = "\n\n".join(paragraph.strip() for _ in range(10))

    chunks = split_markdown(text, target_tokens=100, overlap_tokens=0)

    assert len(chunks) > 1
    # Packed, not one chunk per paragraph.
    assert all(chunk.text.count("\n\n") >= 1 for chunk in chunks[:-1])
    for chunk in chunks:
        assert chunk.token_estimate <= 100 + estimate_tokens(paragraph)


def test_every_paragraph_survives_exactly_once_without_overlap():
    paragraphs = [f"Paragraph number {n} says something." for n in range(12)]

    chunks = split_markdown("\n\n".join(paragraphs), target_tokens=40, overlap_tokens=0)

    joined = "\n\n".join(chunk.text for chunk in chunks)
    for paragraph in paragraphs:
        assert joined.count(paragraph) == 1


def test_a_heading_starts_a_new_chunk():
    text = "\n\n".join(
        [
            "Intro paragraph.",
            "## First section",
            "Body of the first section.",
            "## Second section",
            "Body of the second section.",
        ]
    )

    chunks = split_markdown(text, target_tokens=800, overlap_tokens=0)

    assert [chunk.text.splitlines()[0] for chunk in chunks] == [
        "Intro paragraph.",
        "## First section",
        "## Second section",
    ]


def test_a_fenced_code_block_is_never_split():
    fence = "```bash\n" + "\n".join(f"curl https://example.test/{n}" for n in range(60)) + "\n```"
    text = f"Before the block.\n\n{fence}\n\nAfter the block."

    chunks = split_markdown(text, target_tokens=40, overlap_tokens=0)

    holding = [chunk for chunk in chunks if "```bash" in chunk.text]
    assert len(holding) == 1
    assert holding[0].text.count("```") == 2
    assert fence in holding[0].text


def test_a_tilde_fence_is_honoured_too():
    fence = "~~~\n" + "\n".join(f"line {n}" for n in range(40)) + "\n~~~"
    chunks = split_markdown(f"Before.\n\n{fence}\n\nAfter.", target_tokens=30, overlap_tokens=0)

    holding = [chunk for chunk in chunks if "~~~" in chunk.text]
    assert len(holding) == 1
    assert fence in holding[0].text


def test_a_hash_inside_a_code_fence_is_not_a_heading():
    fence = "```python\n# not a heading\nprint(1)\n```"
    chunks = split_markdown(f"Intro.\n\n{fence}", target_tokens=800, overlap_tokens=0)

    assert len(chunks) == 1
    assert "# not a heading" in chunks[0].text


def test_overlap_carries_the_tail_of_the_previous_chunk():
    paragraphs = [f"Sentence {n} about something interesting and long enough." for n in range(20)]

    chunks = split_markdown("\n\n".join(paragraphs), target_tokens=40, overlap_tokens=10)

    assert len(chunks) > 2
    for previous, following in zip(chunks, chunks[1:], strict=False):
        shared = _shared_boundary(previous.text, following.text)
        assert shared, f"{following.text[:40]!r} does not continue {previous.text[-40:]!r}"
        assert len(shared) <= int(10 * CHARS_PER_TOKEN)


def test_no_chunk_is_nothing_but_the_previous_chunks_tail():
    """Every chunk carries something of its own.

    A chunk consisting of nothing but the previous chunk's tail is a verbatim
    duplicate and a wasted slot in every result list.
    """
    paragraphs = [
        f"Advisory {n} describes a distinct flaw in a distinct product, at length."
        for n in range(12)
    ]

    chunks = split_markdown("\n\n".join(paragraphs), target_tokens=25, overlap_tokens=20)

    for previous, following in zip(chunks, chunks[1:], strict=False):
        assert _shared_boundary(previous.text, following.text) != following.text


def test_chunks_are_numbered_from_zero_and_carry_their_own_estimate():
    text = "\n\n".join(f"Paragraph {n} with a little more text on it." for n in range(15))

    chunks = split_markdown(text, target_tokens=30, overlap_tokens=5)

    assert [chunk.ord for chunk in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert chunk.token_estimate == estimate_tokens(chunk.text)
        assert chunk.text == chunk.text.strip()


def test_a_single_paragraph_longer_than_the_target_is_split_on_words():
    text = "word " * 2000

    chunks = split_markdown(text, target_tokens=100, overlap_tokens=0)

    assert len(chunks) > 5
    for chunk in chunks:
        assert chunk.token_estimate <= 140
    assert "".join(chunk.text for chunk in chunks).replace(" ", "") == text.replace(" ", "")


@pytest.mark.parametrize("bad", [0, -1])
def test_a_nonsense_target_is_refused(bad: int):
    with pytest.raises(ValueError):
        split_markdown("text", target_tokens=bad)


def test_overlap_may_not_reach_the_target():
    with pytest.raises(ValueError):
        split_markdown("text", target_tokens=10, overlap_tokens=10)
