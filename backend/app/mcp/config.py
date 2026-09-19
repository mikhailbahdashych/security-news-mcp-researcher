"""Parse, validate and persist the Claude-Desktop-style ``mcpServers`` blob.

The user pastes the same JSON they would put in ``claude_desktop_config.json``::

    {"mcpServers": {
      "files":  {"command": "npx",
                 "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/scratch"]},
      "remote": {"url": "https://example.com/mcp",
                 "headers": {"Authorization": "Bearer ..."}}
    }}

Two design rules drive everything here.

**Typos must fail loudly.** A server entry is ``extra="forbid"``: ``"comand"``
would otherwise parse into an entry with neither transport, and the user would be
left staring at a server that silently never connects. Likewise an entry with both
``command`` and ``url``, or with neither, is a validation error naming the server.

**Secrets never reach a log.** ``env`` and ``headers`` values are credentials the
user typed. They round-trip to the editor verbatim (this is a single-user local
app and hiding them would make the blob un-editable) but :func:`redact` is the only
shape they may take in an error message or a log line.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import McpServer, McpToolPref, utcnow

#: Server names become part of ``mcp__{server}__{tool}``, which the Anthropic API
#: constrains to ``^[a-zA-Z0-9_-]{1,128}$`` — so constrain them at the door rather
#: than mangling them later and confusing the user about what their server is called.
SERVER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

Transport = Literal["stdio", "http"]


class McpConfigError(ValueError):
    """A user-facing validation failure. The message names the offending server."""


class McpServerEntry(BaseModel):
    """One value of the ``mcpServers`` object, exactly as the user typed it."""

    model_config = ConfigDict(extra="forbid")

    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    #: Not part of Claude Desktop's schema. It is how a server is parked without
    #: deleting it, and ``to_public_json`` only writes it back when it is false, so
    #: a config that never mentions it round-trips unchanged.
    enabled: bool = True

    @model_validator(mode="after")
    def _exactly_one_transport(self) -> McpServerEntry:
        has_command = bool(self.command and self.command.strip())
        has_url = bool(self.url and self.url.strip())
        if has_command and has_url:
            raise ValueError('set either "command" (stdio) or "url" (http), not both')
        if not has_command and not has_url:
            raise ValueError('set either "command" (stdio) or "url" (http)')
        if has_url and not str(self.url).startswith(("http://", "https://")):
            raise ValueError('"url" must start with http:// or https://')
        return self


class McpServerConfig(BaseModel):
    """A validated server, named. This is what the manager connects to.

    Equality is value equality, which is what lets :meth:`McpManager.reload` tell a
    genuinely changed server (drop the connection) from an untouched one (keep it).
    """

    name: str
    transport: Transport
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True

    @classmethod
    def from_entry(cls, name: str, entry: McpServerEntry) -> McpServerConfig:
        stdio = bool(entry.command and entry.command.strip())
        return cls(
            name=name,
            transport="stdio" if stdio else "http",
            command=entry.command,
            args=list(entry.args),
            env=dict(entry.env),
            cwd=entry.cwd,
            url=entry.url,
            headers=dict(entry.headers),
            enabled=entry.enabled,
        )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        """Never render ``env``/``headers`` values: a repr ends up in tracebacks."""
        target = self.command if self.transport == "stdio" else redact_text(self.url or "")
        return (
            f"McpServerConfig(name={self.name!r}, transport={self.transport!r}, "
            f"target={target!r}, enabled={self.enabled!r})"
        )


class SaveResult(BaseModel):
    """What :func:`save_servers` changed, so the route can tell the manager."""

    servers: list[McpServerConfig]
    added: list[str]
    updated: list[str]
    removed: list[str]


#: ``scheme://user:pass@host`` — a credential hiding in a URL rather than in a
#: header, which is what httpx2 and transport error messages echo back.
_USERINFO = re.compile(r"(?<=//)[^/@\s]+@")

#: Everything after a URL's ``?``. Hosted MCP servers routinely authenticate with
#: a token in the query string, so a URL that reaches a log or a UI error has to
#: lose it — ``https://h/mcp?key=s3cret`` is as much a secret as a header is.
_URL_QUERY = re.compile(r"(https?://[^\s\"\'<>]*\?)[^\s\"\'<>]*")


def redact(mapping: dict[str, str]) -> dict[str, str]:
    """Keys kept, values replaced. The only shape ``env``/``headers`` may be logged in."""
    return dict.fromkeys(mapping, "***")


def redact_text(text: str) -> str:
    """Strip the credentials a free-text message can carry: userinfo and queries.

    Used on anything derived from a config or an exception before it reaches a
    log line, a ``repr`` or the UI's error field.
    """
    return _URL_QUERY.sub(r"\1***", _USERINFO.sub("***@", text))


def _first_error(exc: ValidationError) -> str:
    """One readable sentence out of a pydantic error bundle."""
    error = exc.errors()[0]
    if error["type"] == "extra_forbidden":
        field = ".".join(str(part) for part in error["loc"])
        return f'unknown field "{field}"'
    message = error["msg"].removeprefix("Value error, ")
    if error["loc"]:
        field = ".".join(str(part) for part in error["loc"])
        return f'"{field}": {message}'
    return message


def parse_config(raw: Any) -> list[McpServerConfig]:
    """Validate a raw ``{"mcpServers": {...}}`` blob into configs, name-sorted.

    Raises :class:`McpConfigError` with a message that names the offending server.
    """
    if not isinstance(raw, dict):
        raise McpConfigError('The config must be a JSON object with an "mcpServers" key.')

    unknown = sorted(set(raw) - {"mcpServers"})
    if unknown:
        keys = ", ".join(f'"{key}"' for key in unknown)
        raise McpConfigError(f'Unknown top-level key(s): {keys}. Expected only "mcpServers".')

    servers = raw.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise McpConfigError('"mcpServers" must be a JSON object mapping a name to a server.')

    parsed: list[McpServerConfig] = []
    for name, entry in servers.items():
        if not isinstance(name, str) or not SERVER_NAME_PATTERN.match(name):
            raise McpConfigError(
                f'Server "{name}": invalid name. Server names must match '
                "^[A-Za-z0-9_-]{1,64} — the name becomes part of every tool name."
            )
        if not isinstance(entry, dict):
            raise McpConfigError(f'Server "{name}": the entry must be a JSON object.')
        try:
            validated = McpServerEntry.model_validate(entry)
        except ValidationError as exc:
            raise McpConfigError(f'Server "{name}": {_first_error(exc)}.') from exc
        parsed.append(McpServerConfig.from_entry(name, validated))

    return sorted(parsed, key=lambda config: config.name)


def _from_row(row: McpServer) -> McpServerConfig:
    return McpServerConfig(
        name=row.name,
        transport="stdio" if row.transport == "stdio" else "http",
        command=row.command,
        args=list(row.args_json or []),
        env=dict(row.env_json or {}),
        cwd=row.cwd,
        url=row.url,
        headers=dict(row.headers_json or {}),
        enabled=row.enabled,
    )


def _apply(row: McpServer, config: McpServerConfig) -> None:
    row.transport = config.transport
    row.command = config.command
    row.args_json = list(config.args)
    row.env_json = dict(config.env)
    row.cwd = config.cwd
    row.url = config.url
    row.headers_json = dict(config.headers)
    row.enabled = config.enabled


async def load_servers(db: AsyncSession) -> list[McpServerConfig]:
    """Every configured server, name-sorted (the manager's stable ordering)."""
    rows = (await db.execute(select(McpServer).order_by(McpServer.name.asc()))).scalars().all()
    return [_from_row(row) for row in rows]


async def save_servers(db: AsyncSession, raw: Any) -> SaveResult:
    """Replace the whole server set with *raw*, in one transaction.

    Servers that disappear from the blob take their ``mcp_tool_prefs`` rows with
    them: a per-tool toggle for a server that no longer exists would resurface the
    moment the user re-added the server under the same name, which is a surprising
    way to have half a server's tools silently disabled.
    """
    configs = parse_config(raw)
    existing = {row.name: row for row in (await db.execute(select(McpServer))).scalars().all()}

    added: list[str] = []
    updated: list[str] = []
    for config in configs:
        row = existing.get(config.name)
        if row is None:
            row = McpServer(name=config.name)
            _apply(row, config)
            db.add(row)
            added.append(config.name)
            continue
        if _from_row(row) != config:
            _apply(row, config)
            row.updated_at = utcnow()
            updated.append(config.name)

    removed = sorted(set(existing) - {config.name for config in configs})
    if removed:
        await db.execute(delete(McpToolPref).where(McpToolPref.server_name.in_(removed)))
        await db.execute(delete(McpServer).where(McpServer.name.in_(removed)))

    await db.commit()
    return SaveResult(servers=configs, added=added, updated=updated, removed=removed)


def to_public_json(servers: list[McpServerConfig]) -> dict[str, Any]:
    """Rebuild the editable blob. Only non-default keys are written back, so a
    config the user pasted comes back looking like what they pasted."""
    out: dict[str, Any] = {}
    for config in servers:
        entry: dict[str, Any] = {}
        if config.transport == "stdio":
            entry["command"] = config.command
            entry["args"] = list(config.args)
            if config.env:
                entry["env"] = dict(config.env)
            if config.cwd:
                entry["cwd"] = config.cwd
        else:
            entry["url"] = config.url
            if config.headers:
                entry["headers"] = dict(config.headers)
        if not config.enabled:
            entry["enabled"] = False
        out[config.name] = entry
    return {"mcpServers": out}


__all__ = [
    "SERVER_NAME_PATTERN",
    "McpConfigError",
    "McpServerConfig",
    "McpServerEntry",
    "SaveResult",
    "Transport",
    "load_servers",
    "parse_config",
    "redact",
    "redact_text",
    "save_servers",
    "to_public_json",
]
