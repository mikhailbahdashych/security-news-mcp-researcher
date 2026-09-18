"""Compile and the monthly budget (Task 2.5).

``POST /kb/entries/{id}/compile``, ``POST /kb/compile`` and ``GET /kb/budget``.

Its own module, not more routes in ``app/api/kb.py``, so the two Phase 2 tasks that add
knowledge-base routes never share a file (plan decision P2-2).

**404 is the only error these routes have.** A spent budget, a refusal, an
unparseable answer and an entry with no text all come back as a 200 whose
``compiled`` is ``false`` and whose ``reason_code`` says which — never a 500 and
never a 402. Compiling security content with a model that has cyber safeguards
means a refusal is an ordinary Tuesday, and a client that has to read status
codes to tell "the model declined" from "the server is broken" will get it wrong.

The client is built from :data:`~app.api.deps.ChatClientFactory` rather than
taken as a dependency: ``get_anthropic_client`` reads the key on the request's
own session and never commits, so the read transaction it opens would still be
open while the compile waits on the API — the one thing ``app/kb/capture.py``'s
header asks nobody to do.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import AppSettings, ChatClientFactory, KbServiceDep
from app.kb import compile as compile_module
from app.kb.models import KbEntry
from app.kb.service import EntryFacts, KbService
from app.schemas.kb import (
    BudgetRead,
    CompileBatchResponse,
    CompileEntity,
    CompileEstimateResponse,
    CompileNewTopic,
    CompileRequest,
    CompileResponse,
    EntryRead,
)

router = APIRouter(prefix="/kb", tags=["kb"])


async def _load(kb: KbService, entry_id: int) -> KbEntry:
    entry = await kb.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Entry not found")
    return entry


async def _response(kb: KbService, result: compile_module.CompileResult) -> CompileResponse:
    """One result, with the entry **re-read** so the page sees the new summary."""
    entry = await _load(kb, result.entry_id)
    facts = (await kb.facts([entry])).get(entry.id, EntryFacts())
    return CompileResponse(
        entry=EntryRead.build(entry, facts),
        compiled=result.compiled,
        reason=result.skipped_reason,
        reason_code=result.reason_code,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        model=result.model,
        prompt_version=result.prompt_version,
        new_topic=CompileNewTopic(**result.new_topic) if result.new_topic else None,
        suggested_topic_ids=result.topic_ids,
        suggested_tags=result.tags,
        entities=[CompileEntity(kind=kind, value=value) for kind, value in result.entities],
    )


@router.post("/entries/{entry_id}/compile", response_model=CompileResponse)
async def compile_entry(
    entry_id: int,
    kb: KbServiceDep,
    client_factory: ChatClientFactory,
    app_settings: AppSettings,
) -> CompileResponse:
    """Summarise one entry and store the answer."""
    await _load(kb, entry_id)
    result = await compile_module.compile_entry(
        kb.session_factory,
        client_factory,
        entry_id,
        settings=app_settings,
        embedder=kb.embedder,
        source="user",
    )
    return await _response(kb, result)


@router.post("/compile", response_model=None)
async def compile_batch(
    payload: CompileRequest,
    kb: KbServiceDep,
    client_factory: ChatClientFactory,
    app_settings: AppSettings,
    estimate: Annotated[bool, Query()] = False,
) -> CompileEstimateResponse | CompileBatchResponse:
    """Compile several entries, or price the batch without compiling any.

    The estimate uses ``messages.count_tokens`` on the prompt the batch *would*
    send. That endpoint generates no completion and bills nothing, so the price
    of asking "what would this cost" is never itself a cost.
    """
    if estimate:
        return await _estimate(payload, kb, client_factory, app_settings)

    results = []
    for entry_id in payload.entry_ids:
        await _load(kb, entry_id)
        results.append(
            await _response(
                kb,
                await compile_module.compile_entry(
                    kb.session_factory,
                    client_factory,
                    entry_id,
                    settings=app_settings,
                    embedder=kb.embedder,
                    source="batch",
                ),
            )
        )
    return CompileBatchResponse(results=results)


async def _estimate(
    payload: CompileRequest,
    kb: KbService,
    client_factory: ChatClientFactory,
    app_settings: AppSettings,
) -> CompileEstimateResponse:
    client = await compile_module.build_client(kb.session_factory, client_factory, app_settings)
    try:
        counted = await compile_module.estimate_batch(kb.session_factory, client, payload.entry_ids)
    finally:
        if client is not None:
            await client.close()

    limit = await compile_module.monthly_budget(kb.session_factory)
    usage = await compile_module.month_usage(kb.session_factory)
    remaining = max(0, limit - usage["anthropic_input"] - usage["anthropic_output"])
    return CompileEstimateResponse(
        entries=counted["entries"],
        input_tokens=counted["input_tokens"],
        budget_remaining=remaining,
        would_exceed=counted["input_tokens"] > remaining,
    )


@router.get("/budget", response_model=BudgetRead)
async def budget(kb: KbServiceDep) -> BudgetRead:
    """Month-to-date compile spend against the monthly budget."""
    limit = await compile_module.monthly_budget(kb.session_factory)
    usage = await compile_module.month_usage(kb.session_factory)
    total = usage["anthropic_input"] + usage["anthropic_output"]
    return BudgetRead(
        month=usage["month"],
        limit=limit,
        anthropic_input=usage["anthropic_input"],
        anthropic_output=usage["anthropic_output"],
        anthropic_total=total,
        remaining=max(0, limit - total),
        exhausted=total >= limit,
        # Never added to the Anthropic figures: a different provider, a different
        # unit, and this one is our estimate rather than a billed number.
        voyage=usage["voyage"],
    )


__all__ = ["router"]
