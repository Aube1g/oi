#!/usr/bin/env python3
"""MCP server configuration — reading other agents' files.

XLI bundles its own MCP servers, but the interesting ones are written by other
people and configured for other clients. Claude Code keeps them in `.mcp.json`
and `~/.claude.json`; Codex keeps them in `~/.codex/config.toml`. This module
reads all of those and produces one shape, so an XLI user can point the agent
at a server they already configured for Claude Code or Codex and have it work
without retyping anything.

The formats, for reference:

    Claude Code, project or user scope:
        {"mcpServers": {"name": {"command": "npx",
                                 "args": ["-y", "@some/server"],
                                 "env": {"KEY": "value"}}}}

    Codex, ~/.codex/config.toml:
        [mcp_servers.name]
        command = "npx"
        args = ["-y", "@some/server"]
        startup_timeout_sec = 20
        tool_timeout_sec = 60
        enabled = true
        enabled_tools = ["search"]
        disabled_tools = ["delete"]

    XLI itself accepts both, plus the same keys as Codex:
        {"mcpServers": {...}}     in ~/.xli/mcp.json or <project>/.mcp.json

Keys that matter for behaviour are carried through: `enabled`, `enabled_tools`,
`disabled_tools` and the two timeouts. A server that declares `enabled = false`
stays off; a server with `enabled_tools` exposes only those.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.mcp.config")

#: Where a user's own MCP servers are looked for, in order. Later files win, so
#: a project's `.mcp.json` overrides the user's global set, which is what every
#: other client does.
USER_FILES = (
    "~/.xli/mcp.json",
    "~/.claude.json",
    "~/.claude/mcp.json",
    "~/.codex/config.toml",
)
PROJECT_FILES = (".mcp.json", ".xli/mcp.json")


@dataclass(slots=True)
class ExternalServer:
    """One server somebody else configured, in a shape we can launch."""

    name: str
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    transport: str = "stdio"
    enabled: bool = True
    enabled_tools: tuple[str, ...] = ()
    disabled_tools: tuple[str, ...] = ()
    startup_timeout: float = 20.0
    tool_timeout: float = 60.0
    source: str = ""

    @property
    def kind(self) -> str:
        """`stdio` or `http`, for display and for choosing a transport."""
        if self.url:
            return "http"
        return self.transport

    def describe(self) -> str:
        if self.url:
            return self.url
        return " ".join(self.command)

    def to_info(self) -> dict[str, Any]:
        """The registry's server-info shape, so the rest of XLI is unchanged."""
        return {
            "description": f"external ({self.source})",
            "tools": list(self.enabled_tools),
            "enabled": self.enabled,
            "external": True,
            "command": list(self.command),
            "env": dict(self.env),
            "url": self.url,
            "transport": self.kind,
            "enabled_tools": list(self.enabled_tools),
            "disabled_tools": list(self.disabled_tools),
            "startup_timeout": self.startup_timeout,
            "tool_timeout": self.tool_timeout,
            "source": self.source,
        }


# --------------------------------------------------------------------- toml
def _parse_toml(text: str) -> dict[str, Any]:
    """`tomllib` when the interpreter has it, and a small reader when not.

    XLI supports Python 3.10, where `tomllib` does not exist and `tomli` may
    not be installed. The fallback understands exactly what a Codex config
    needs: tables, string/array/boolean/number values, and comments. It is not
    a TOML parser and says so rather than pretending.
    """
    try:
        import tomllib  # type: ignore[import-not-found]

        return tomllib.loads(text)
    except ModuleNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 - a broken file must not stop the scan
        logger.log_structured("WARN", "mcp.config", f"config.toml unreadable: {exc}")
        return {}

    root: dict[str, Any] = {}
    current: dict[str, Any] = root
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            path = [part.strip().strip("\"'") for part in line[1:-1].split(".")]
            current = root
            for part in path:
                current = current.setdefault(part, {})
            continue
        if "=" not in line:
            continue
        key, _, raw = line.partition("=")
        current[key.strip().strip("\"'")] = _toml_value(raw.strip())
    return root


def _toml_value(raw: str) -> Any:
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_toml_value(part.strip()) for part in inner.split(",") if part.strip()]
    if raw.startswith("{") and raw.endswith("}"):
        out: dict[str, str] = {}
        for part in raw[1:-1].split(","):
            key, _, value = part.partition("=")
            if key.strip():
                out[key.strip().strip("\"'")] = str(_toml_value(value.strip()))
        return out
    lowered = raw.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


# ------------------------------------------------------------------ reading
def _server_from_entry(entry: Any, source: str) -> ExternalServer | None:
    """Claude/Codex entry -> ExternalServer, or None when it is not one."""
    if not isinstance(entry, dict):
        return None

    command = entry.get("command") or ""
    args = entry.get("args") or []
    url = str(entry.get("url") or entry.get("serverUrl") or "")
    env = entry.get("env") or {}

    if isinstance(command, str):
        command_list = [command, *[str(a) for a in args]] if command else []
    elif isinstance(command, list):
        command_list = [str(part) for part in command]
    else:
        command_list = []

    if not command_list and not url:
        return None

    transport = str(entry.get("type") or entry.get("transport") or "")
    if url and not transport:
        transport = "http"

    def _tools(key: str) -> tuple[str, ...]:
        value = entry.get(key) or entry.get(_camel(key)) or []
        if isinstance(value, str):
            return (value,)
        if isinstance(value, (list, tuple)):
            return tuple(str(item) for item in value)
        return ()

    return ExternalServer(
        name="",
        command=command_list,
        env={str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {},
        url=url,
        transport=transport or "stdio",
        enabled=bool(entry.get("enabled", True)),
        enabled_tools=_tools("enabled_tools"),
        disabled_tools=_tools("disabled_tools"),
        startup_timeout=float(entry.get("startup_timeout_sec") or entry.get("startupTimeoutSec") or 20.0),
        tool_timeout=float(entry.get("tool_timeout_sec") or entry.get("toolTimeoutSec") or 60.0),
        source=source,
    )


def _camel(key: str) -> str:
    head, *rest = key.split("_")
    return head + "".join(part.title() for part in rest)


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.log_structured("WARN", "mcp.config", f"{path} unreadable: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _servers_in(data: dict[str, Any]) -> dict[str, Any]:
    """Both the Claude spelling (`mcpServers`) and a bare mapping."""
    for key in ("mcpServers", "mcp_servers", "mcp"):
        value = data.get(key)
        if isinstance(value, dict):
            return value
    # A file that is itself a mapping of servers, as some projects write.
    if all(isinstance(value, dict) for value in data.values()) and data:
        return data
    return {}


def discover(start: Path | None = None, extra_paths: tuple[str, ...] = ()) -> dict[str, ExternalServer]:
    """Every externally configured MCP server, later sources winning.

    `start` is the directory to walk up from looking for project files (the
    current working directory by default). A project file in a parent
    directory is found, so `xli` run in a subdirectory of a project still sees
    the project's MCP servers.
    """
    found: dict[str, ExternalServer] = {}
    paths: list[Path] = []

    for pattern in USER_FILES:
        paths.append(Path(os.path.expanduser(pattern)))
    for pattern in extra_paths:
        paths.append(Path(os.path.expanduser(pattern)))

    # Project files, nearest last (so the nearest project wins), stopping at
    # the first directory that has one.
    here = (start or Path.cwd()).resolve()
    for directory in [here, *here.parents]:
        matches = [directory / name for name in PROJECT_FILES if (directory / name).exists()]
        if matches:
            paths.extend(matches)
            break

    for path in paths:
        if not path.exists():
            continue
        if path.suffix == ".toml":
            data = _parse_toml(path.read_text(encoding="utf-8", errors="replace"))
        else:
            data = _read_json_file(path)
        raw = data.get("mcp_servers") if isinstance(data.get("mcp_servers"), dict) else _servers_in(data)
        if not isinstance(raw, dict):
            continue
        for name, entry in raw.items():
            server = _server_from_entry(entry, str(path))
            if server is None:
                continue
            server.name = str(name)
            found[server.name] = server

    if found:
        logger.log_structured(
            "INFO", "mcp.config", f"found {len(found)} external MCP servers: {', '.join(sorted(found))}"
        )
    return found


def allowed_tools(info: dict[str, Any], available: list[str]) -> list[str]:
    """Apply `enabled_tools` / `disabled_tools` to a server's tool list.

    Other clients write these to keep a wide server (filesystem, Notion) from
    flooding the model with tools it will never use. Honouring them is what
    makes "the config I already have" true rather than approximately true.
    """
    enabled = info.get("enabled_tools") or []
    disabled = set(info.get("disabled_tools") or [])
    out = [name for name in available if name not in disabled]
    if enabled:
        out = [name for name in out if name in set(enabled)]
    return out
