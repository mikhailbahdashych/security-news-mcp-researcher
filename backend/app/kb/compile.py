"""Compile: one entry's snapshot turned into a summary and suggestions.

One button, one Anthropic call, one row in the trail. The rules that are easy to
get wrong, and why each one is here:

* **Budget first, call second.** The monthly ceiling is checked *before* the
  request, never after it, so hitting it costs nothing. A refusal to spend is a
  ``budget_hit`` row and an ordinary 200 — not an error.
* **The budget is compile spend only.** ``month_usage`` sums ``kb_activity``
  rows whose action is ``compile``/``recompile`` for Anthropic and ``embed`` for
  Voyage, and the two counters are **never added together**: one is tokens
  Anthropic billed, the other is this application's own estimate of what Voyage
  read. Chat spend is counted per session and is deliberately outside all of it.
* **No transaction is held across the call.** Everything the request needs is
  read and the session closed before the client is built; the answer is written
  in a new one. SQLite has a single writer and this call takes seconds.
* **The model's answer is untrusted.** Topic ids it invented are dropped, tags
  and entities are capped in count and in length, entity kinds are checked
  against the column's own CHECK constraint, and ``new_topic`` creates nothing —
  the user confirms it through the topic route that already exists.
* **The summary is never evidence.** It is stored as a ``summary`` chunk so an
  entry-level match is cheap, and both search legs default to
  ``chunk_kinds=('body',)``, so nothing the model wrote is ever quoted back to a
  model as source material (spec §4.7).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import anthropic
from anthropic import AsyncAnthropic
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.oneshot import OneshotError, RefusalError, structured_call_result
from app.config import Settings
from app.db.models import utcnow
from app.kb import capture as capture_module
from app.kb.chunking import estimate_tokens
from app.kb.embeddings import Embedder
from app.kb.models import (
    KbActivity,
    KbChunk,
    KbEntry,
    KbEntryEntity,
    KbEntryTag,
    KbEntryTopic,
    KbSnapshot,
    Topic,
)
from app.kb.prompts import (
    COMPILE_PROMPT_VERSION,
    COMPILE_SCHEMA,
    COMPILE_SYSTEM,
    MAX_ENTITIES,
    MAX_ENTITY_CHARS,
    MAX_TAGS,
    render_compile_user,
)
from app.services import settings as settings_service

logger = logging.getLogger(__name__)

#: The entity kinds a *model* may propose. CVE ids already came from the regex
#: at capture time, and ``kb_entry_entities.kind`` has a CHECK constraint — an
#: invented kind must be dropped here rather than raise on the insert.
MODEL_ENTITY_KINDS = ("vendor", "product")

#: How long a tag may be. Not a column limit; a limit on what one answer can do
#: to the tag list.
MAX_TAG_CHARS = 60


@dataclass(frozen=True, slots=True)
class CompileResult:
    """What one compile did — or did not do, and why.

    ``compiled=False`` is an outcome and not an error: budget, refusal, an
    unusable answer and an entry with no text all come back this way, with
    ``reason_code`` naming which. The route turns every one of them into a 200.
    """

    entry_id: int
    compiled: bool
    summary_md: str | None = None
    #: Existing topics the model chose, with the ids it invented already dropped.
    topic_ids: list[int] = field(default_factory=list)
    #: A proposal. Nothing is created from it; the user confirms it.
    new_topic: dict[str, Any] | None = None
    tags: list[str] = field(default_factory=list)
    entities: list[tuple[str, str]] = field(default_factory=list)
    #: ``usage.input_tokens`` **plus both cache counters** — see :func:`month_usage`.
    input_tokens: int = 0
    output_tokens: int = 0
    #: The model that answered, which a fallback switch can change.
    model: str | None = None
    prompt_version: int | None = None
    #: The sentence shown to the user; ``None`` on success.
    skipped_reason: str | None = None
    #: One of ``budget``/``refusal``/``parse``/``no_text``/``api_error``.
    reason_code: str | None = None
    #: True when the suggestions above were written onto the entry as well as
    #: reported (``kb_auto_accept_suggestions``).
    applied: bool = False


@dataclass(frozen=True, slots=True)
class _Plan:
    """Everything one compile reads from the database *before* it calls out."""

    entry_id: int
    system: str
    user: str
    model: str
    effort: str
    auto_accept: bool
    #: The entry already had a summary, so the trail calls this a ``recompile``.
    recompile: bool


# ------------------------------------------------------------------ reading


async def _plan(session_factory: async_sessionmaker[AsyncSession], entry_id: int) -> _Plan | None:
    """Build the request from the entry's current snapshot, or ``None``.

    ``None`` means "there is nothing to compile" — an unknown entry, or one whose
    snapshot is empty. The session is closed before anything outbound happens,
    which is the whole reason this is a separate function.
    """
    async with session_factory() as session:
        entry = await session.get(KbEntry, entry_id)
        if entry is None:
            return None
        text = ""
        if entry.current_snapshot_id is not None:
            snapshot = await session.get(KbSnapshot, entry.current_snapshot_id)
            text = snapshot.text if snapshot is not None else ""
        if not text.strip():
            return None

        topics = [
            (topic_id, name)
            for topic_id, name in (
                await session.execute(select(Topic.id, Topic.name).order_by(Topic.name))
            ).all()
        ]
        # Read once, before the call, exactly as ``providers.turn_settings`` does
        # for a chat turn — never inside a loop over entries.
        model = await settings_service.get_str(session, "kb_compile_model")
        effort = await settings_service.get_choice(session, "kb_compile_effort")
        prompt = await settings_service.get_str(session, "kb_compile_prompt")
        max_chars = await settings_service.get_int(session, "kb_compile_max_chars")
        auto_accept = await settings_service.get_bool(session, "kb_auto_accept_suggestions")
        user = render_compile_user(
            title=entry.title,
            url=entry.url,
            published_at=entry.published_at,
            text=text,
            topics=topics,
            prompt=prompt,
            max_chars=max_chars,
        )
        return _Plan(
            entry_id=entry_id,
            system=COMPILE_SYSTEM,
            user=user,
            model=model,
            effort=effort,
            auto_accept=auto_accept,
            recompile=entry.compiled_at is not None,
        )


async def build_client(
    session_factory: async_sessionmaker[AsyncSession],
    client_factory: Callable[[str], AsyncAnthropic],
    settings: Settings | None = None,
) -> AsyncAnthropic | None:
    """A client from the effective key, or ``None`` when none is configured.

    The factory rather than ``get_anthropic_client``: that dependency reads the
    key on the *request's* session and never commits, so the read transaction it
    opens would still be open while this waits on the API. The caller closes what
    it gets back.
    """
    async with session_factory() as session:
        api_key = await settings_service.get_effective_api_key(session, settings)
    return client_factory(api_key) if api_key else None


# ------------------------------------------------------------------ budget


async def month_usage(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    """Month-to-date knowledge-base spend, from ``kb_activity``.

    The calendar month is UTC, from :func:`app.db.models.utcnow` — every
    ``DATETIME`` in this database is naive UTC, so a local ``datetime.now()``
    would move the boundary by the developer's timezone.

    ``anthropic_input`` is the **sum of all three input counters**
    (``input_tokens`` + ``cache_creation_input_tokens`` + ``cache_read_input_tokens``),
    because that is how :func:`compile_entry` stores it: a one-shot call sets no
    ``cache_control`` so today the two cache counters are zero, and the day
    somebody adds caching the budget must not silently start under-counting.

    ``voyage`` is **this application's estimate** (``ceil(chars / 3.6)`` per
    embedded batch), not a figure Voyage billed. It is reported beside the
    Anthropic numbers and never added to them.
    """
    start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    async with session_factory() as session:
        anthropic_row = (
            await session.execute(
                select(
                    func.coalesce(func.sum(KbActivity.input_tokens), 0),
                    func.coalesce(func.sum(KbActivity.output_tokens), 0),
                ).where(
                    KbActivity.action.in_(("compile", "recompile")),
                    KbActivity.at >= start,
                )
            )
        ).one()
        voyage = await session.scalar(
            select(func.coalesce(func.sum(KbActivity.input_tokens), 0)).where(
                KbActivity.action == "embed", KbActivity.at >= start
            )
        )
    return {
        "month": start.strftime("%Y-%m"),
        "anthropic_input": int(anthropic_row[0] or 0),
        "anthropic_output": int(anthropic_row[1] or 0),
        "voyage": int(voyage or 0),
    }


async def monthly_budget(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """``kb_compile_monthly_token_budget``. Zero really means "spend nothing"."""
    async with session_factory() as session:
        return await settings_service.get_int(session, "kb_compile_monthly_token_budget")


async def budget_allows(session_factory: async_sessionmaker[AsyncSession], estimate: int) -> bool:
    """Would spending *estimate* more compile tokens stay inside the month's budget?"""
    limit = await monthly_budget(session_factory)
    usage = await month_usage(session_factory)
    spent = usage["anthropic_input"] + usage["anthropic_output"]
    return spent + max(0, estimate) <= limit


async def estimate_batch(
    session_factory: async_sessionmaker[AsyncSession],
    client: AsyncAnthropic | None,
    entry_ids: Sequence[int],
) -> dict[str, int]:
    """What "Compile N" would cost, without compiling anything.

    ``messages.count_tokens`` is free and is not a model call — no completion is
    generated and nothing is billed — so the estimate can be exact. With no key
    configured, or when the count fails, the local ``ceil(chars / 3.6)`` estimate
    stands in rather than the request failing: an estimate the user cannot get is
    worse than one that is approximate.
    """
    entries = 0
    total = 0
    for entry_id in entry_ids:
        plan = await _plan(session_factory, entry_id)
        if plan is None:
            continue
        entries += 1
        total += await _count_tokens(client, plan)
    return {"entries": entries, "input_tokens": total}


async def _count_tokens(client: AsyncAnthropic | None, plan: _Plan) -> int:
    if client is not None:
        try:
            counted = await client.beta.messages.count_tokens(
                model=plan.model,
                system=plan.system,
                messages=[{"role": "user", "content": plan.user}],
            )
            return int(getattr(counted, "input_tokens", 0))
        # Anything at all: this route is documented to have no error path but
        # 404, and the local ceil(chars / 3.6) estimate is a perfectly good
        # answer. A 500 from "what would this cost" is the one reply that helps
        # nobody.
        except Exception as exc:  # noqa: BLE001 - an estimate always answers
            logger.warning("Counting tokens for entry %s failed: %s", plan.entry_id, exc)
    return estimate_tokens(plan.system + plan.user)


# ----------------------------------------------------------------- compiling


async def compile_entry(
    session_factory: async_sessionmaker[AsyncSession],
    client_factory: Callable[[str], AsyncAnthropic],
    entry_id: int,
    *,
    settings: Settings | None = None,
    embedder: Embedder | None = None,
    source: str = "compile",
) -> CompileResult:
    """Summarise one entry and store the answer. Never raises into a route."""
    plan = await _plan(session_factory, entry_id)
    if plan is None:
        return await _skip(
            session_factory,
            entry_id,
            "compile",
            code="no_text",
            reason="There is no text on this entry to compile.",
            source=source,
        )

    # Before the call, always: the check is worthless the moment it is made after
    # the tokens have already been spent.
    estimate = estimate_tokens(plan.system + plan.user)
    if not await budget_allows(session_factory, estimate):
        return await _skip(
            session_factory,
            entry_id,
            "budget_hit",
            code="budget",
            reason="The monthly compile token budget is spent.",
            source=source,
            detail=f"needs about {estimate} tokens",
        )

    client = await build_client(session_factory, client_factory, settings)
    if client is None:
        return await _skip(
            session_factory,
            entry_id,
            "compile",
            code="api_error",
            reason="No Anthropic API key is configured.",
            source=source,
        )

    action = "recompile" if plan.recompile else "compile"
    try:
        result = await structured_call_result(
            client,
            model=plan.model,
            effort=plan.effort,
            system=plan.system,
            user=plan.user,
            schema=COMPILE_SCHEMA,
        )
    except RefusalError as exc:
        # Billed like any other 200: the tokens go in the trail so the month's
        # budget moves, or a feed the model always declines spends forever.
        return await _skip(
            session_factory,
            entry_id,
            action,
            code="refusal",
            reason=str(exc),
            source=source,
            model=plan.model,
            detail=f"refused ({exc.category or 'no category'}): {exc}",
            usage=exc.usage,
        )
    except OneshotError as exc:
        # Both the unparseable answer and the one that ran out of output tokens:
        # either way nothing usable came back, and neither is a 500. The
        # max_tokens one billed the whole output budget, so it is charged too.
        return await _skip(
            session_factory,
            entry_id,
            action,
            code="parse",
            reason=str(exc),
            source=source,
            model=plan.model,
            detail=f"unusable answer: {exc}",
            usage=exc.usage,
        )
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
        # The SDK's exceptions are deliberately not wrapped by ``oneshot``, so
        # this is where a 429 or a dead connection becomes an outcome.
        return await _skip(
            session_factory,
            entry_id,
            action,
            code="api_error",
            reason=str(exc),
            source=source,
            model=plan.model,
            detail=f"api error: {type(exc).__name__}: {exc}",
        )
    finally:
        await client.close()

    return await _store(
        session_factory, plan, result, action=action, source=source, embedder=embedder
    )


async def compile_if_auto(
    session_factory: async_sessionmaker[AsyncSession],
    client_factory: Callable[[str], AsyncAnthropic],
    entry_id: int,
    **kwargs: Any,
) -> CompileResult | None:
    """Compile when ``kb_compile_mode`` is ``auto``; otherwise do nothing.

    The capture triggers call this, never :func:`compile_entry` — the mode is
    policy, and policy read at the call site is policy the next call site
    forgets. ``None`` means "the mode said no", which is not a failure.
    """
    async with session_factory() as session:
        mode = await settings_service.get_choice(session, "kb_compile_mode")
    if mode != "auto":
        return None
    return await compile_entry(session_factory, client_factory, entry_id, **kwargs)


# ------------------------------------------------------------------ writing


def _usage_tokens(usage: dict[str, Any] | None) -> tuple[int, int]:
    """One turn's ``(input, output)`` as the budget counts them.

    The input figure is **all three input counters** summed
    (``input_tokens`` + ``cache_creation_input_tokens`` + ``cache_read_input_tokens``),
    which is how :func:`month_usage` reads them back. ``structured_call`` sets no
    ``cache_control``, so the two cache figures are zero today — adding them
    anyway is what keeps the budget honest if that ever changes.
    """
    usage = usage or {}
    return (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
    )


async def _store(
    session_factory: async_sessionmaker[AsyncSession],
    plan: _Plan,
    result: Any,
    *,
    action: str,
    source: str,
    embedder: Embedder | None,
) -> CompileResult:
    """Apply one answer: the summary, its chunk, the entities and the suggestions."""
    data = result.data if isinstance(result.data, dict) else {}
    input_tokens, output_tokens = _usage_tokens(result.usage)

    summary = str(data.get("summary_md") or "").strip()
    wanted_topic_ids = _ints(data.get("topic_ids"))
    tags = _tags(data.get("tags"))
    entities = _entities(data.get("entities"))
    new_topic = _new_topic(data.get("new_topic"))
    now = utcnow()

    async with session_factory() as session:
        entry = await session.get(KbEntry, plan.entry_id)
        if entry is None:  # purged while the model was thinking
            return CompileResult(
                entry_id=plan.entry_id,
                compiled=False,
                reason_code="no_text",
                skipped_reason="The entry was deleted while it was being compiled.",
            )

        entry.summary_md = summary
        entry.compiled_at = now
        # The model that *answered*: a fallback switch is a normal 200 with a
        # different model, and the requested one is never what wrote this.
        entry.compile_model = result.model
        entry.compile_prompt_version = COMPILE_PROMPT_VERSION
        entry.compile_input_tokens = input_tokens
        entry.compile_output_tokens = output_tokens
        entry.updated_at = now

        # The previous summary chunk goes first: ``_rechunk`` only ever touches
        # ``kind='body'``, so nothing else will clean this one up.
        await session.execute(
            delete(KbChunk).where(KbChunk.entry_id == entry.id, KbChunk.kind == "summary")
        )
        if summary:
            session.add(
                KbChunk(
                    entry_id=entry.id,
                    snapshot_id=None,
                    ord=0,
                    text=summary,
                    token_estimate=estimate_tokens(summary),
                    kind="summary",
                )
            )

        held = {
            (kind, value)
            for kind, value in (
                await session.execute(
                    select(KbEntryEntity.kind, KbEntryEntity.value).where(
                        KbEntryEntity.entry_id == entry.id
                    )
                )
            ).all()
        }
        for kind, value in entities:
            if (kind, value) in held:
                continue
            # ``_sync_regex_entities`` only deletes ``source='regex'`` rows, so a
            # later refresh leaves these alone.
            session.add(KbEntryEntity(entry_id=entry.id, kind=kind, value=value, source="model"))
            held.add((kind, value))

        # A topic the user deleted while the model was answering is dropped in
        # silence: the answer is not wrong, it is just out of date.
        known = set(
            (await session.execute(select(Topic.id).where(Topic.id.in_(wanted_topic_ids))))
            .scalars()
            .all()
        )
        known_topic_ids = [topic_id for topic_id in wanted_topic_ids if topic_id in known]

        if plan.auto_accept:
            await _apply(session, entry.id, known_topic_ids, tags, now)

        await capture_module.log_activity(
            session,
            action,
            entry_id=entry.id,
            source=source,
            model=result.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            detail=json.dumps(
                {
                    "applied": plan.auto_accept,
                    "topic_ids": known_topic_ids,
                    "tags": tags,
                    "new_topic": new_topic,
                }
            ),
        )
        await session.commit()

    # Outside the transaction, and after the commit: an embedder call is a
    # network round trip, and a failure leaves the chunk pending — which is the
    # state the next "Embed now" resumes from — rather than losing the summary.
    if summary and embedder is not None and embedder.dimensions > 0:
        try:
            await capture_module.embed_pending(
                session_factory, embedder, entry_id=plan.entry_id, source=source
            )
        except Exception:  # noqa: BLE001 - the summary is stored either way
            logger.exception("Embedding the summary of entry %s failed", plan.entry_id)

    return CompileResult(
        entry_id=plan.entry_id,
        compiled=True,
        summary_md=summary,
        topic_ids=known_topic_ids,
        new_topic=new_topic,
        tags=tags,
        entities=entities,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model=result.model,
        prompt_version=COMPILE_PROMPT_VERSION,
        applied=plan.auto_accept,
    )


async def _apply(
    session: AsyncSession,
    entry_id: int,
    topic_ids: Sequence[int],
    tags: Sequence[str],
    now: datetime,
) -> None:
    """Write the suggestions onto the entry, **still marked ``suggested``**.

    Not ``KbService.set_topics``: that one replaces the set and marks everything
    confirmed, which is right for a user's edit and wrong for a model's. These
    rows apply immediately *and* stay reviewable, and an existing confirmed row
    is never downgraded to a suggestion.
    """
    existing_topics = set(
        (
            await session.execute(
                select(KbEntryTopic.topic_id).where(KbEntryTopic.entry_id == entry_id)
            )
        )
        .scalars()
        .all()
    )
    for topic_id in topic_ids:
        if topic_id in existing_topics:
            continue
        session.add(KbEntryTopic(entry_id=entry_id, topic_id=topic_id, suggested=True))
        topic = await session.get(Topic, topic_id)
        if topic is not None:
            topic.last_used_at = now

    existing_tags = set(
        (await session.execute(select(KbEntryTag.tag).where(KbEntryTag.entry_id == entry_id)))
        .scalars()
        .all()
    )
    for tag in tags:
        if tag in existing_tags:
            continue
        session.add(KbEntryTag(entry_id=entry_id, tag=tag, suggested=True))


async def _skip(
    session_factory: async_sessionmaker[AsyncSession],
    entry_id: int,
    action: str,
    *,
    code: str,
    reason: str,
    source: str,
    model: str | None = None,
    detail: str | None = None,
    usage: dict[str, Any] | None = None,
) -> CompileResult:
    """Record why nothing was compiled, and answer with it.

    ``kb_activity.entry_id`` is a foreign key, so a row about an entry that does
    not exist — compile called on an unknown id, or one purged mid-call — is
    written without the reference rather than raising on the insert.

    *usage* is the tokens the attempt **cost**, and a failure is not free: a
    refusal is an HTTP 200 Anthropic bills and a ``max_tokens`` stop bills the
    output as well. It is ``None`` only where nothing was spent — a spent budget,
    a missing key, an entry with no text, a connection that never answered.
    """
    input_tokens, output_tokens = _usage_tokens(usage)
    async with session_factory() as session:
        known = await session.get(KbEntry, entry_id) is not None
        await capture_module.log_activity(
            session,
            action,
            entry_id=entry_id if known else None,
            source=source,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            detail=detail or reason,
        )
        await session.commit()
    return CompileResult(entry_id=entry_id, compiled=False, skipped_reason=reason, reason_code=code)


# ------------------------------------------------- reading the model's answer


def _ints(values: Any) -> list[int]:
    """The integers in *values*, in order, deduplicated. Anything else is dropped."""
    out: list[int] = []
    for value in values if isinstance(values, list) else []:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(out))


def _tags(values: Any) -> list[str]:
    out: list[str] = []
    for value in values if isinstance(values, list) else []:
        tag = str(value).strip()[:MAX_TAG_CHARS]
        if tag:
            out.append(tag)
    return list(dict.fromkeys(out))[:MAX_TAGS]


def _entities(values: Any) -> list[tuple[str, str]]:
    """The vendors and products the model named — capped like the tags are.

    Twice over, as :func:`_tags` is: the schema asks for at most
    :data:`MAX_ENTITIES` of at most :data:`MAX_ENTITY_CHARS` each, and a model
    that ignores either bound still cannot write more rows than that. An invented
    ``kind`` is dropped here rather than raising on the insert, because
    ``kb_entry_entities.kind`` has a CHECK constraint.
    """
    out: list[tuple[str, str]] = []
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, dict):
            continue
        kind = str(value.get("kind") or "").strip().lower()
        text = str(value.get("value") or "").strip()[:MAX_ENTITY_CHARS]
        if kind in MODEL_ENTITY_KINDS and text:
            out.append((kind, text))
    return list(dict.fromkeys(out))[:MAX_ENTITIES]


def _new_topic(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    name = str(value.get("name") or "").strip()
    if not name:
        return None
    description = value.get("description")
    return {
        "name": name,
        "description": str(description).strip() if description else None,
    }


__all__ = [
    "MODEL_ENTITY_KINDS",
    "CompileResult",
    "budget_allows",
    "build_client",
    "compile_entry",
    "compile_if_auto",
    "estimate_batch",
    "month_usage",
    "monthly_budget",
]
