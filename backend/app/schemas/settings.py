"""Request/response models for the settings and models endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Effort = Literal["low", "medium", "high", "xhigh", "max"]
ThinkingDisplay = Literal["summarized", "omitted"]
KeySource = Literal["env", "stored", "none"]


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
    #: Whether a key is stored **in this database** — not whether one is usable.
    has_api_key: bool
    api_key_masked: str
    #: Which source the effective key comes from: ``"env"`` (the process
    #: environment or ``.env``, which overrides the stored one), ``"stored"``, or
    #: ``"none"``. Lets the Settings page explain a working app with no stored key.
    key_source: KeySource
    web_search_enabled: bool
    web_search_max_uses: int
    web_fetch_enabled: bool
    max_tool_turns: int
    note_template: str
    system_prompt_extra: str
    feed_timeout_s: int


class SettingsUpdate(BaseModel):
    """A partial update: every field is optional and only what is sent is written.

    ``anthropic_api_key`` is write-only — it is accepted here and never returned by
    any endpoint. Sending an empty string clears the stored key.
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


class TestKeyResult(BaseModel):
    """Outcome of a live credential check against the Anthropic API."""

    ok: bool
    error: str | None = None


class ModelOption(BaseModel):
    """One entry of the model picker."""

    id: str
    display_name: str


__all__ = [
    "Effort",
    "KeySource",
    "ModelOption",
    "SettingsRead",
    "SettingsUpdate",
    "TestKeyResult",
    "ThinkingDisplay",
]
