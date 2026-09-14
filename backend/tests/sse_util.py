"""Parsing SSE bodies in tests.

Shared by the chat-stream and notes-generation suites so there is one definition
of "what the browser would have seen".
"""

from __future__ import annotations

import json


def parse_sse(body: str) -> list[tuple[str, dict]]:
    """Parse a raw SSE body into ``(event, payload)`` pairs, ignoring heartbeats."""
    parsed: list[tuple[str, dict]] = []
    for frame in body.replace("\r\n", "\n").split("\n\n"):
        name = "message"
        data: list[str] = []
        for line in frame.split("\n"):
            if not line or line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                name = value
            elif field == "data":
                data.append(value)
        if data:
            parsed.append((name, json.loads("\n".join(data))))
    return parsed


def event_names(body: str) -> list[str]:
    return [name for name, _ in parse_sse(body)]


def payloads_for(body: str, event: str) -> list[dict]:
    return [payload for name, payload in parse_sse(body) if name == event]


__all__ = ["event_names", "parse_sse", "payloads_for"]
