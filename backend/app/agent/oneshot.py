"""One tool-free, persistence-free structured call.

**The one exception to "every LLM call goes through ``runner.run``".**
``run()`` is an ``AsyncIterator[AgentEvent]`` and has no structured-output
parameter — there is no path by which a caller receives a *parsed object* back
from it. Compile needs one, so this module makes the same request the runner
makes (same betas, same ``fallbacks``, the same
``stop_reason``-before-``content`` rule) minus tools, minus persistence, minus
``cache_control``, plus ``output_config.format``. Nothing else in the app may
call ``messages.create``/``stream`` directly.

Four rules that are easy to get wrong:

* **Never enable citations on a structured call.** ``citations: {"enabled":
  true}`` on a ``document`` or ``search_result`` block *together with* an
  ``output_config.format`` is a 400. That includes anything a later phase adds.
* **No ``cache_control``.** A one-shot call over a unique article writes a cache
  entry at the 1.25x surcharge and never reads it back. Nothing here is
  prompt-cache prefix.
* **Read ``stop_reason`` before ``content``.** A refusal is an HTTP 200 whose
  ``content`` can be ``[]``, and this app compiles *security* content, so a
  refusal is a first-class state rather than an edge case.
* **Only the text after the last ``fallback`` block is the answer.** The same
  safeguards that produce a refusal also produce a mid-output model switch, and
  the abandoned model's half-written JSON stays in ``content`` ahead of the
  boundary. :func:`app.agent.runner.fallback_boundary` finds it.

``thinking`` is deliberately absent: omitting it runs adaptive thinking with the
display defaulted to ``"omitted"``, which is what a call whose output nobody
watches stream wants. ``budget_tokens`` would be a 400 anyway.

Callers catch **two** families: :class:`OneshotError` for an answer that did not
come back as an object, and ``anthropic.APIStatusError`` for the transport — the
SDK's exceptions are deliberately let through rather than wrapped, so a 429 is
still a 429 to whoever decides whether to retry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from anthropic import AsyncAnthropic

# _usage_dict is the runner's; the budget accounting downstream has to agree
# with the chat path's counters byte for byte, so it is imported rather than
# re-derived. fallback_boundary is the runner's for the same reason: "where did
# the model switch" is one rule, not two.
from app.agent.runner import (
    FALLBACK_BETA,
    FALLBACKS,
    MAX_TOKENS,
    _usage_dict,
    fallback_boundary,
)

#: How much of an unparseable answer travels in the error. Never the whole body:
#: the caller logs it.
PARSE_PREVIEW_CHARS = 200


class OneshotError(Exception):
    """A structured call that did not come back with an object."""


class RefusalError(OneshotError):
    """``stop_reason == "refusal"`` — the model declined, with a category."""

    def __init__(
        self,
        message: str,
        *,
        category: str | None = None,
        explanation: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.explanation = explanation


class StructuredParseError(OneshotError):
    """The model answered, but not with a JSON object."""


@dataclass(frozen=True, slots=True)
class StructuredResult:
    data: dict[str, Any]
    usage: dict[str, Any]
    #: The model that actually answered. A ``fallbacks`` switch is a normal 200
    #: whose model differs from the requested one — never compare the two.
    model: str
    stop_reason: str | None


async def structured_call_result(
    client: AsyncAnthropic,
    *,
    model: str,
    effort: str,
    system: str,
    user: str,
    schema: dict[str, Any],
) -> StructuredResult:
    """Ask for one JSON object and return it with the turn's usage and model."""
    request: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "betas": [FALLBACK_BETA],
        "fallbacks": FALLBACKS,
        "output_config": {
            "effort": effort,
            "format": {"type": "json_schema", "schema": schema},
        },
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    # Streamed, not created: max_tokens this size on a non-streaming request
    # risks the SDK's own HTTP timeout. Nobody watches the events, so the final
    # message is all this waits for.
    async with client.beta.messages.stream(**request) as stream:
        final = await stream.get_final_message()

    stop_reason = getattr(final, "stop_reason", None)

    if stop_reason == "refusal":
        # stop_details exists only here, and content is not touched at all:
        # a refusal's content list is empty.
        details = getattr(final, "stop_details", None)
        category = getattr(details, "category", None)
        explanation = getattr(details, "explanation", None)
        raise RefusalError(
            explanation or f"The model declined this request (category: {category}).",
            category=category,
            explanation=explanation,
        )
    if stop_reason == "max_tokens":
        raise OneshotError(f"The answer hit the {MAX_TOKENS}-token output limit before finishing.")
    if stop_reason != "end_turn":
        # A tool-free call has nothing to resume and nothing to dispatch, so
        # pause_turn and tool_use are as unexpected as an unknown reason.
        raise OneshotError(f"Unexpected stop_reason: {stop_reason!r}")

    # Only the blocks after the last ``fallback`` are the answer. A turn can
    # switch models mid-output — the whole point of the beta on a module that
    # compiles security articles — and the abandoned model's half-written JSON
    # is still in ``content`` ahead of the boundary. Joining both sides yields
    # invalid JSON out of a perfectly good 200.
    blocks = list(getattr(final, "content", None) or [])
    text = "".join(
        getattr(block, "text", "") or ""
        for block in blocks[fallback_boundary(blocks) + 1 :]
        if getattr(block, "type", None) == "text"
    )
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise StructuredParseError(
            f"The answer was not JSON: {text[:PARSE_PREVIEW_CHARS]!r}"
        ) from exc
    if not isinstance(data, dict):
        raise StructuredParseError(
            f"The answer was JSON but not an object: {text[:PARSE_PREVIEW_CHARS]!r}"
        )

    return StructuredResult(
        data=data,
        usage=_usage_dict(getattr(final, "usage", None)),
        model=getattr(final, "model", model),
        stop_reason=stop_reason,
    )


async def structured_call(
    client: AsyncAnthropic,
    *,
    model: str,
    effort: str,
    system: str,
    user: str,
    schema: dict[str, Any],
) -> dict[str, Any]:
    """``structured_call_result`` when only the parsed object is wanted."""
    result = await structured_call_result(
        client, model=model, effort=effort, system=system, user=user, schema=schema
    )
    return result.data


__all__ = [
    "PARSE_PREVIEW_CHARS",
    "OneshotError",
    "RefusalError",
    "StructuredParseError",
    "StructuredResult",
    "structured_call",
    "structured_call_result",
]
