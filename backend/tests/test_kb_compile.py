"""Compile, its controls and the monthly budget.

Every Anthropic call here goes through ``ScriptedAnthropic``
(``tests/fakes/anthropic.py``), whose turns are real ``anthropic.types.beta``
objects — a refusal really is an HTTP 200 with ``content == []``. Nothing in this
file reaches the network, and the budget assertions are made against the
``kb_activity`` rows the code actually wrote rather than against its return
value, because the budget is defined over that table.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.kb import compile as compile_module
from app.kb.capture import capture_article, log_activity
from app.kb.models import KbActivity, KbChunk, KbEntry, KbEntryTag, KbEntryTopic, Topic
from app.kb.prompts import (
    COMPILE_PROMPT_VERSION,
    COMPILE_SCHEMA,
    COMPILE_SYSTEM,
    DEFAULT_COMPILE_PROMPT,
    MAX_TAGS,
    render_compile_user,
)
from app.kb.service import KbService
from app.services import settings as settings_service
from tests.fakes.anthropic import ScriptedAnthropic, turn_refusal, turn_text, turn_text_with_usage

BODY = (
    "# The xz backdoor\n\n"
    "A malicious commit in liblzma introduced a backdoor tracked as CVE-2024-3094. "
    "The payload hooks RSA_public_decrypt through the IFUNC resolver, which is why it "
    "only activates inside an sshd process.\n\n"
    "Debian and Fedora shipped the affected build in their unstable channels only. "
    "Downgrading liblzma to 5.4.6 removes the payload entirely.\n"
)

ANSWER = {
    "summary_md": "- A backdoor reached liblzma.\n- Downgrade to 5.4.6.",
    "topic_ids": [],
    "new_topic": None,
    "tags": ["supply-chain"],
    "entities": [{"kind": "product", "value": "liblzma"}],
}


def answer(**overrides) -> str:
    """The model's JSON answer, with fields replaced."""
    return json.dumps({**ANSWER, **overrides})


# --------------------------------------------------------------- the prompt


def test_the_default_compile_prompt_and_its_version_move_together():
    """The text and the number live in one file but are read from two.

    A stored summary records the version that wrote it, so changing the text
    without changing the number makes that record a lie. This test is the
    tripwire: edit the prompt, this fails, bump the version and update the hash.
    """
    digest = hashlib.sha256(DEFAULT_COMPILE_PROMPT.encode("utf-8")).hexdigest()

    assert (COMPILE_PROMPT_VERSION, digest[:16]) == (1, "f2153b2c4470afd1")


def test_the_compile_schema_forbids_additional_properties_and_caps_tags():
    # Both are API requirements rather than taste: a json_schema format without
    # `additionalProperties: false` and a complete `required` list is a 400.
    assert COMPILE_SCHEMA["additionalProperties"] is False
    assert set(COMPILE_SCHEMA["required"]) == set(COMPILE_SCHEMA["properties"])
    assert COMPILE_SCHEMA["properties"]["tags"]["maxItems"] == MAX_TAGS
    # `new_topic` is nullable rather than absent, because every key is required.
    assert COMPILE_SCHEMA["properties"]["new_topic"]["type"] == ["object", "null"]
    assert COMPILE_SCHEMA["properties"]["new_topic"]["additionalProperties"] is False


def test_the_system_prompt_names_the_article_as_data_and_carries_no_company_context():
    lowered = COMPILE_SYSTEM.lower()

    assert "data, not instruction" in lowered
    # The shipped prompts stay generic — no employer, team or product context.
    assert "employer" not in lowered
    assert "company" not in lowered


def test_the_user_message_truncates_on_characters_and_lists_the_topics():
    rendered = render_compile_user(
        title="The xz backdoor",
        url="https://example.test/xz",
        published_at=None,
        text="q" * 500,
        topics=[(3, "Supply chain"), (7, "Linux")],
        prompt="Summarise it.",
        max_chars=100,
    )

    assert rendered.startswith("Summarise it.")
    assert "3 — Supply chain" in rendered
    assert "7 — Linux" in rendered
    assert rendered.count("q") == 100
    assert "truncated" in rendered
