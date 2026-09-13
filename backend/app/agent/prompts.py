"""The system prompt.

Deliberately short. A long, rule-stacked prompt written for older models measurably
degrades Opus 5 output, and the prompt sits at the front of the cache prefix, so it
must also be byte-stable across every request in a session: no timestamps, no
per-request ids, no "today is ..." line.
"""

from __future__ import annotations

DEFAULT_SYSTEM_PROMPT = """You are a security-news research assistant for one \
engineer's local inbox.

Search the local feed inbox first — it is curated and current — and reach for the \
web when the inbox does not have the answer. Cite the source URL inline for every \
claim you make. Be concrete: name CVE IDs, affected versions and the remediation. \
If something is unknown or unconfirmed, say so plainly rather than filling the gap."""


def build_system_prompt(*, override: str | None = None, extra: str = "") -> str:
    """The prompt for one turn.

    *override* **replaces** the default outright (Task 6's note generation passes a
    template prompt); *extra* — the ``system_prompt_extra`` setting — is appended in
    both cases.
    """
    base = DEFAULT_SYSTEM_PROMPT if override is None else override
    extra = (extra or "").strip()
    if not extra:
        return base
    return f"{base}\n\n{extra}"


__all__ = ["DEFAULT_SYSTEM_PROMPT", "build_system_prompt"]
