#!/usr/bin/env python3
"""Shared helpers for the bundled stdio MCP servers.

Every server under xli/mcp/servers/ is a standalone script with the same shape:
a TOOLS dict of name -> function, and a handle_request that dispatches
tools/list and tools/call. This module holds the one piece they all need, so
the protocol stays consistent across them instead of drifting per file.

The reason inputSchema matters: callers cannot guess a tool's arguments. The
MCP bridge used to send every tool the same {"query": ..., "code": ...} bag,
and most tools rejected it with "got an unexpected keyword argument", so the
bridge's pre/post steps silently produced nothing. With the schema published,
a caller sends only what the tool declares.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _json_type(annotation: Any) -> str:
    """Map a Python annotation to a JSON Schema type, defaulting to string."""
    if annotation is inspect.Parameter.empty:
        return "string"
    return _TYPE_MAP.get(annotation, "string")


def input_schema(fn: Callable) -> dict[str, Any]:
    """Derive a JSON Schema for one tool from its signature."""
    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in inspect.signature(fn).parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        properties[name] = {"type": _json_type(param.annotation)}
        if param.default is inspect.Parameter.empty:
            required.append(name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


def tool_descriptors(tools: dict[str, Callable]) -> list[dict[str, Any]]:
    """The tools/list payload: each tool with its real argument schema."""
    return [
        {"name": name, "inputSchema": input_schema(fn)} for name, fn in tools.items()
    ]


def filter_arguments(fn: Callable, arguments: dict[str, Any]) -> dict[str, Any]:
    """Drop arguments the tool does not declare, so a shared param bag works.

    A caller that does not know the schema can hand over everything it has and
    let the tool take what it needs, instead of failing on the first extra key.
    """
    accepted = {
        name
        for name, param in inspect.signature(fn).parameters.items()
        if param.kind not in (param.VAR_POSITIONAL, param.VAR_KEYWORD)
    }
    has_var_keyword = any(
        param.kind is param.VAR_KEYWORD
        for param in inspect.signature(fn).parameters.values()
    )
    if has_var_keyword:
        return dict(arguments)
    return {key: value for key, value in arguments.items() if key in accepted}


# ---------------------------------------------------------------- handshake
#: The protocol revision the bundled servers announce. Kept as a literal: the
#: bundled servers are stdlib-only by policy, and importing the transport (and
#: through it the logger, and through that the whole core) would drag a
#: dependency graph larger than the servers themselves.
PROTOCOL_VERSION = "2025-06-18"


def shake_hands(request: dict[str, Any]) -> dict[str, Any] | None:
    """Answer the protocol methods every MCP client sends before tool calls.

    `initialize` is not optional in the protocol, and a server that answers it
    with "unknown method" looks broken to a strict client (and to `xli mcp
    test`). The bundled servers grew up talking to XLI's own client, which
    never asked — the same reason `notifications/initialized` was ignored.

    Returns the reply, or None when the request is not the protocol layer's
    business and the server should handle it the way it always has.
    """
    method = (request or {}).get("method")
    request_id = (request or {}).get("id")

    if method == "initialize":
        params = (request or {}).get("params") or {}
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": str(params.get("protocolVersion") or PROTOCOL_VERSION),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "xli", "version": _version()},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return None  # the server builds its own, from its own tool table
    if isinstance(method, str) and method.startswith("notifications/"):
        # A notification gets no reply by protocol. An id-less response is
        # ignored by every client (it matches no pending request), which is
        # cheaper than teaching each server's loop to stay silent.
        return {"jsonrpc": "2.0", "id": None, "result": None}
    return None


def _version() -> str:
    try:
        from xli.version import VERSION

        return str(VERSION)
    except Exception:  # noqa: BLE001 - a version string is not worth a failure
        return "0"
