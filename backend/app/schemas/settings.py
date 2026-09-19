"""Request/response models for the settings and models endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.kb.embeddings import EMBEDDING_MODELS

Effort = Literal["low", "medium", "high", "xhigh", "max"]
ThinkingDisplay = Literal["summarized", "omitted"]
#: ``manual`` — entries wait for a "Compile N entries" click; ``auto`` — compile
#: at capture time.
CompileMode = Literal["manual", "auto"]


class KbSchemaVersionRead(BaseModel):
    """What the knowledge base's two virtual tables were actually built with.

    Read-only, and not a preference: the Settings panel compares it with the
    constants in this build and offers a rebuild when they disagree.
    """

    version: int
    vec_ddl_version: int
    vec_dimensions: int
    fts_ddl_version: int
    tokenizer: str


class SettingsRead(BaseModel):
    """The settings as the UI sees them — note there is no raw API key field.

    ``effort`` and ``thinking_display`` are the same closed sets ``SettingsUpdate``
    accepts, so the contract the frontend's union types describe is the one this
    endpoint actually keeps. The store behind them is untyped TEXT, so the route
    falls a hand-edited value back to its default rather than answering 500 — see
    ``app.api.settings._one_of``.
    """

    model: str
    effort: Effort
    thinking_display: ThinkingDisplay
    #: Whether a key is stored in this database, which is the only place one can
    #: be: there is no environment override. ``has_api_key`` is the whole truth.
    has_api_key: bool
    api_key_masked: str
    web_search_enabled: bool
    web_search_max_uses: int
    web_fetch_enabled: bool
    max_tool_turns: int
    note_template: str
    system_prompt_extra: str
    feed_timeout_s: int
    #: The knowledge base's capture policy. Manual saves are always allowed.
    kb_capture_starred: bool
    kb_capture_notes: bool
    kb_min_snapshot_chars: int
    kb_schema_version: KbSchemaVersionRead
    #: Whether a Voyage key is stored in this database, the only place one can
    #: be. Mirrors the Anthropic key above.
    has_voyage_key: bool
    voyage_api_key_masked: str
    kb_embedding_model: str
    #: What ``kb_embedding_model`` may be set to, read-only: the Settings select is
    #: built from this rather than from a hard-coded list, so the embedder's table
    #: stays the one source. A value stored by hand (or by a newer build) is still
    #: reported verbatim above and simply is not in here — the page can then show
    #: what is configured *and* what is accepted, instead of hiding one of them.
    kb_embedding_models: list[str]
    kb_capture_findings: bool
    kb_compile_mode: CompileMode
    kb_compile_model: str
    kb_compile_effort: Effort
    kb_compile_prompt: str
    #: The prompt this build **ships** with, read-only, so "Reset to default"
    #: restores that and not whatever was saved last (plan decision P2-24). It is
    #: on the wire nowhere else, and the client cannot reconstruct it.
    kb_compile_prompt_default: str
    kb_compile_max_chars: int
    #: Compile tokens per calendar month. **Chat spend is not counted here.**
    kb_compile_monthly_token_budget: int
    kb_auto_accept_suggestions: bool
    kb_reviewed_only: bool
    kb_recency_boost: bool
    #: Read by nobody until the reranker lands (Phase 3).
    kb_rerank: bool
    kb_duplicate_threshold: float


class SettingsUpdate(BaseModel):
    """A partial update: every field is optional and only what is sent is written.

    ``anthropic_api_key`` and ``voyage_api_key`` are write-only — accepted here and
    never returned by any endpoint. Sending an empty string clears the stored key.
    """

    model_config = ConfigDict(extra="forbid")

    anthropic_api_key: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    effort: Effort | None = None
    thinking_display: ThinkingDisplay | None = None
    web_search_enabled: bool | None = None
    web_search_max_uses: int | None = Field(default=None, ge=1, le=100)
    web_fetch_enabled: bool | None = None
    max_tool_turns: int | None = Field(default=None, ge=1, le=100)
    note_template: str | None = Field(default=None, max_length=20_000)
    system_prompt_extra: str | None = Field(default=None, max_length=20_000)
    feed_timeout_s: int | None = Field(default=None, ge=1, le=300)
    kb_capture_starred: bool | None = None
    kb_capture_notes: bool | None = None
    kb_min_snapshot_chars: int | None = Field(default=None, ge=0, le=100_000)
    voyage_api_key: str | None = Field(default=None, max_length=500)
    kb_embedding_model: str | None = Field(default=None, min_length=1, max_length=200)
    kb_capture_findings: bool | None = None
    kb_compile_mode: CompileMode | None = None
    kb_compile_model: str | None = Field(default=None, min_length=1, max_length=200)
    kb_compile_effort: Effort | None = None
    kb_compile_prompt: str | None = Field(default=None, max_length=20_000)
    kb_compile_max_chars: int | None = Field(default=None, ge=1_000, le=200_000)
    kb_compile_monthly_token_budget: int | None = Field(default=None, ge=0, le=1_000_000_000)
    kb_auto_accept_suggestions: bool | None = None
    kb_reviewed_only: bool | None = None
    kb_recency_boost: bool | None = None
    kb_rerank: bool | None = None
    kb_duplicate_threshold: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("kb_compile_prompt")
    @classmethod
    def _prompt_is_not_blank(cls, value: str | None) -> str | None:
        """A blank compile prompt is refused, not stored (plan decision P2-24).

        The getter falls back to the shipped default only when the **row is
        absent**, so saving an empty string destroyed the default for good and
        every compile afterwards went out with no instructions at all. Checked
        after stripping, because a textarea holding one newline is empty.
        """
        if value is not None and not value.strip():
            raise ValueError("the compile prompt cannot be empty")
        return value

    @field_validator("kb_embedding_model")
    @classmethod
    def _model_is_one_the_embedder_knows(cls, value: str | None) -> str | None:
        """Only a model this build can batch for (``EMBEDDING_MODELS``).

        Not a cosmetic check: ``update_settings`` reads a *changed* model as
        "throw the vector index away", so a typo emptied ``kb_chunk_vec``, marked
        every chunk pending and left Embed now 502-ing on Voyage's 400 — with a
        full paid re-embed as the only way back. The 422 lands before the write.
        Kept as a validator rather than a ``Literal`` so the set stays derived
        from the embedder's own table and cannot drift from it.
        """
        if value is not None and value not in EMBEDDING_MODELS:
            raise ValueError(f"must be one of {', '.join(EMBEDDING_MODELS)}")
        return value


class TestKeyResult(BaseModel):
    """Outcome of a live credential check against the Anthropic API."""

    ok: bool
    error: str | None = None


class ModelOption(BaseModel):
    """One entry of the model picker."""

    id: str
    display_name: str


__all__ = [
    "CompileMode",
    "Effort",
    "KbSchemaVersionRead",
    "ModelOption",
    "SettingsRead",
    "SettingsUpdate",
    "TestKeyResult",
    "ThinkingDisplay",
]
