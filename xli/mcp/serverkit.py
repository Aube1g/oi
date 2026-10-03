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
