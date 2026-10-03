#!/usr/bin/env python3
"""
XLI Call summaries — one human line per tool call.

Raw `{"name": ..., "args": {...}}` JSON on a transcript line is noise: the eye
wants *what is being touched*, not the serialisation. Each built-in tool gets
a shape; unknown tools fall back to compact JSON. Shared by the CLI, the REPL
and the TUI so a call reads the same everywhere.
"""

from __future__ import annotations

from typing import Any

from xli.ui.text import json_dumps, truncate


def summarise_call(name: str, args: dict[str, Any] | None) -> str:
    """A short human-readable line for one tool call."""
    args = args or {}

    if name == "read":
        where = str(args.get("path", "?"))
        offset = int(args.get("offset", 1) or 1)
        limit = int(args.get("limit", 0) or 0)
        tail = ""
        if offset > 1:
            tail += f" c {offset}"
        if limit:
            tail += f" +{limit}"
        return f"{where}{tail}"

    if name in ("write", "edit"):
        where = args.get("path")
        if where:
            return str(where)
        # No path key: preview what is being written rather than a bare "?",
        # which used to tell the user nothing at all.
        for key in ("content", "new", "old", "text", "code"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return truncate(value.strip().splitlines()[0], 60)
        return "?"

    if name == "grep":
        pattern = str(args.get("pattern", "?"))
        where = args.get("path")
        return f"{pattern!r}" + (f" в {where}" if where else "")

    if name == "glob":
        pattern = str(args.get("pattern", "*"))
        where = args.get("path")
        return pattern + (f" в {where}" if where else "")

    if name == "ls":
        return str(args.get("path", "."))

    if name == "bash":
        return truncate(str(args.get("command", "?")), 80)

    if name == "git":
        git_args = args.get("args")
        if isinstance(git_args, list):
            return truncate(" ".join(str(a) for a in git_args), 80)
        return truncate(str(git_args or args), 80)

    if name == "todo":
        action = str(args.get("action", "?"))
        text = str(args.get("text", "")).strip()
        return f"{action}" + (f" {text!r}" if text else "")

    if name == "think":
        return truncate(str(args.get("thought", "?")), 120)

    return truncate(json_dumps(args), 100)
