"""Markdown-aware chunking. Pure: no I/O, no database, no clock.

Paragraph-first, because a retrieved passage that stops mid-sentence reads as
noise to both the model and the user. Blocks are packed up to a target size, a
heading always begins a new chunk, a fenced code block is never cut in half, and
each chunk carries the tail of the one before it so an answer that straddles a
boundary is still findable from either side.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

#: Characters per token. **Not 4.** Security prose is dense with hashes, CVE ids,
#: version strings and inline code, all of which tokenize far worse than English,
#: and ``chars/4`` under-counts it. The same estimate is what
#: ``kb_compile_max_chars`` and the "Compile N" estimate are compared against, so
#: there is exactly one of these numbers in the application.
CHARS_PER_TOKEN = 3.6

_HEADING = re.compile(r"^ {0,3}#{1,6}(\s|$)")
_FENCE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})")


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrievable passage of an entry's text."""

    ord: int
    text: str
    token_estimate: int


def estimate_tokens(text: str) -> int:
    """``ceil(len(text) / 3.6)`` — see :data:`CHARS_PER_TOKEN`."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


@dataclass(frozen=True, slots=True)
class _Block:
    text: str
    #: A heading forces a chunk boundary; a code block is indivisible.
    starts_chunk: bool
    atomic: bool


def _blocks(text: str) -> list[_Block]:
    """Split into headings, fenced code blocks and paragraphs, in order."""
    blocks: list[_Block] = []
    paragraph: list[str] = []
    fence: list[str] = []
    closing: str | None = None

    def flush_paragraph() -> None:
        if paragraph:
            body = "\n".join(paragraph).strip()
            if body:
                blocks.append(_Block(text=body, starts_chunk=False, atomic=False))
            paragraph.clear()

    for line in text.splitlines():
        if closing is not None:
            fence.append(line)
            if line.strip().startswith(closing):
                blocks.append(_Block(text="\n".join(fence), starts_chunk=False, atomic=True))
                fence.clear()
                closing = None
            continue

        opening = _FENCE.match(line)
        if opening:
            flush_paragraph()
            closing = opening.group("fence")
            fence.append(line)
            continue

        if not line.strip():
            flush_paragraph()
            continue

        if _HEADING.match(line):
            flush_paragraph()
            blocks.append(_Block(text=line.strip(), starts_chunk=True, atomic=False))
            continue

        paragraph.append(line)

    if closing is not None:
        # An unterminated fence: keep it whole rather than pretend it is prose.
        blocks.append(_Block(text="\n".join(fence), starts_chunk=False, atomic=True))
    flush_paragraph()
    return blocks


def _split_long_block(block: _Block, target_tokens: int) -> list[_Block]:
    """Break a paragraph that is bigger than a whole chunk, on word boundaries.

    A code block is never broken: the point of keeping it whole is that half a
    command or half a diff is worse than a chunk over budget.
    """
    limit = int(target_tokens * CHARS_PER_TOKEN)
    if block.atomic or len(block.text) <= limit:
        return [block]

    pieces: list[_Block] = []
    current = ""
    for word in block.text.split(" "):
        candidate = f"{current} {word}" if current else word
        if current and len(candidate) > limit:
            pieces.append(_Block(text=current, starts_chunk=False, atomic=False))
            current = word
        else:
            current = candidate
    if current:
        pieces.append(_Block(text=current, starts_chunk=False, atomic=False))
    return pieces


def _tail(text: str, overlap_tokens: int) -> str:
    """The last ``overlap_tokens`` worth of *text*, snapped to a word boundary."""
    if overlap_tokens <= 0:
        return ""
    wanted = int(overlap_tokens * CHARS_PER_TOKEN)
    if wanted >= len(text):
        return text
    tail = text[-wanted:]
    cut = tail.find(" ")
    return tail[cut + 1 :].strip() if cut != -1 else tail.strip()


def split_markdown(text: str, *, target_tokens: int = 800, overlap_tokens: int = 80) -> list[Chunk]:
    """Split *text* into overlapping, heading-aware chunks of about the target size.

    Empty or whitespace-only text yields no chunks at all — an entry with nothing
    in it is not searchable and must not pretend to be.
    """
    if target_tokens < 1:
        raise ValueError(f"target_tokens must be positive, got {target_tokens}")
    if overlap_tokens < 0:
        raise ValueError(f"overlap_tokens must not be negative, got {overlap_tokens}")
    if overlap_tokens >= target_tokens:
        raise ValueError("overlap_tokens must be smaller than target_tokens")

    blocks: list[_Block] = []
    for block in _blocks(text):
        blocks.extend(_split_long_block(block, target_tokens))
    if not blocks:
        return []

    chunks: list[Chunk] = []
    current: list[str] = []
    budget = 0
    carried = 0

    def flush() -> None:
        nonlocal current, budget, carried
        if not current:
            return
        body = "\n\n".join(current).strip()
        chunks.append(Chunk(ord=len(chunks), text=body, token_estimate=estimate_tokens(body)))
        overlap = _tail(body, overlap_tokens)
        current = [overlap] if overlap else []
        carried = estimate_tokens(overlap) if overlap else 0
        budget = carried

    for block in blocks:
        size = estimate_tokens(block.text)
        # "This chunk holds something of its own, not just the tail it inherited."
        # Defensive: a flush is always followed by an append, so the pathological
        # case cannot arise today — but a chunk that is nothing but the previous
        # chunk's tail is a verbatim duplicate and a wasted slot in every result
        # list, which is worth one comparison to make impossible by construction.
        has_own_content = budget > carried
        starts_over = block.starts_chunk and has_own_content
        too_big = has_own_content and budget + size > target_tokens
        if starts_over or too_big:
            flush()
        current.append(block.text)
        budget += size

    if budget > carried:
        body = "\n\n".join(current).strip()
        chunks.append(Chunk(ord=len(chunks), text=body, token_estimate=estimate_tokens(body)))

    return chunks


__all__ = ["CHARS_PER_TOKEN", "Chunk", "estimate_tokens", "split_markdown"]
