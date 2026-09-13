"""Talking to the Anthropic Models API: listing models and checking a key.

The model list changes at most a few times a year but the settings page asks for
it on every visit, so results are cached in-process for an hour. The cache is
keyed on a digest of the API key that produced them, which means changing the key
(stored or via ``ANTHROPIC_API_KEY``) transparently invalidates it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import UTC, datetime
from typing import Any, Protocol

import anthropic

from app.schemas.settings import ModelOption, TestKeyResult

logger = logging.getLogger(__name__)

MODEL_CACHE_TTL_SECONDS = 3600.0

_EPOCH = datetime.min.replace(tzinfo=UTC)


class AnthropicLike(Protocol):
    """The slice of ``anthropic.AsyncAnthropic`` this module uses."""

    models: Any


async def fetch_models(client: AnthropicLike) -> list[ModelOption]:
    """Every model the key can see, newest first.

    Iterating the paginator (rather than reading ``.data``) follows every page.
    """
    found: list[tuple[datetime, ModelOption]] = []
    async for item in client.models.list():
        created_at = getattr(item, "created_at", None) or _EPOCH
        found.append((created_at, ModelOption(id=item.id, display_name=item.display_name)))

    found.sort(key=lambda entry: entry[0], reverse=True)
    return [option for _, option in found]


async def check_api_key(client: AnthropicLike | None) -> TestKeyResult:
    """Verify a key by making the cheapest authenticated call there is.

    Never surfaces the SDK's own message, which can echo request details; the short
    messages below are safe to show in the UI.
    """
    if client is None:
        return TestKeyResult(ok=False, error="no API key configured")

    try:
        await fetch_models(client)
    except anthropic.AuthenticationError:
        return TestKeyResult(ok=False, error="invalid API key")
    except anthropic.PermissionDeniedError:
        return TestKeyResult(ok=False, error="this key is not allowed to use the API")
    except anthropic.RateLimitError:
        return TestKeyResult(ok=False, error="rate limited — try again in a moment")
    except anthropic.APIStatusError as error:
        return TestKeyResult(ok=False, error=f"Anthropic API error (HTTP {error.status_code})")
    except anthropic.APIConnectionError:
        return TestKeyResult(ok=False, error="could not reach the Anthropic API")

    return TestKeyResult(ok=True, error=None)


class ModelCache:
    """A single-entry, time-limited cache of the model list for one API key."""

    def __init__(self, ttl_seconds: float = MODEL_CACHE_TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        self._lock = asyncio.Lock()
        self._fingerprint: str | None = None
        self._models: list[ModelOption] = []
        self._expires_at = 0.0

    async def get(self, api_key: str, client: AnthropicLike) -> list[ModelOption]:
        """Cached models for ``api_key``, fetching through ``client`` on a miss."""
        fingerprint = _fingerprint(api_key)
        async with self._lock:
            if self._fingerprint == fingerprint and time.monotonic() < self._expires_at:
                return list(self._models)

            models = await fetch_models(client)
            self._fingerprint = fingerprint
            self._models = models
            self._expires_at = time.monotonic() + self._ttl_seconds
            return list(models)

    def invalidate(self) -> None:
        self._fingerprint = None
        self._models = []
        self._expires_at = 0.0


def _fingerprint(api_key: str) -> str:
    """A digest, so the process never keeps the raw key in a module global."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


model_cache = ModelCache()


async def list_models(client: AnthropicLike | None, api_key: str) -> list[ModelOption]:
    """The model picker's options; empty when there is no key or the API refuses.

    An empty list is a normal UI state (the page falls back to a free-text model
    field), so an API failure here is logged rather than raised — the Test key
    button is what tells the user *why* their key is not working.
    """
    if client is None:
        return []
    try:
        return await model_cache.get(api_key, client)
    except anthropic.APIError as error:
        logger.warning("Could not list Anthropic models: %s", type(error).__name__)
        return []


__all__ = [
    "MODEL_CACHE_TTL_SECONDS",
    "ModelCache",
    "check_api_key",
    "fetch_models",
    "list_models",
    "model_cache",
]
