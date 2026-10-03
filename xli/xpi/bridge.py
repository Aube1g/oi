#!/usr/bin/env python3
"""
XPI Bridge — Python ↔ Lua RPC for Neovim.

Lets an in-process XPI plugin talk to the editor: call into Lua, or receive
calls coming the other way.

The one thing this file must get right is quoting. The original built Lua
source by interpolating a JSON payload straight into an f-string, so any
plugin whose data contained a quote or a backslash produced broken Lua — and
data shaped deliberately could execute arbitrary code in the editor. Arguments
are now passed through `vim.json.decode` on a JSON string built by a JSON
encoder, so plugin data is never parsed as Lua.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.xpi.bridge")


class XpiBridge:
    """RPC bridge between Python and Lua in Neovim."""

    def __init__(self, nvim: Any = None):
        self.nvim = nvim
        self._handlers: dict[str, Callable[..., Any]] = {}

    # --------------------------------------------------------------- outbound
    def register_handler(self, method: str, handler: Callable[..., Any]) -> None:
        """Register a Python function Lua can call."""
        self._handlers[method] = handler

    def has_handler(self, method: str) -> bool:
        return method in self._handlers

    def call_lua(self, code: str) -> Any:
        """Execute literal Lua source. Only for trusted, static snippets."""
        if self.nvim is None:
            logger.log_structured("WARN", "xli.bridge", "no nvim connection")
            return None
        try:
            return self.nvim.exec_lua(code)
        except Exception as exc:  # noqa: BLE001 - the editor may be gone
            logger.log_error("xli.bridge", "lua call failed", exc=exc)
            return None

    def notify_lua(self, event: str, data: dict[str, Any] | None = None) -> Any:
        """Send an event to the Lua side.

        The payload crosses as a JSON *string* and is decoded in Lua, so event
        names and data are never interpreted as code.
        """
        if self.nvim is None:
            logger.log_structured("WARN", "xli.bridge", "no nvim connection")
            return None

        try:
            payload = json.dumps({"event": event, "data": data or {}}, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            logger.log_error("xli.bridge", f"cannot serialize event {event!r}", exc=exc)
            return None

        # json.dumps already escaped quotes and backslashes; encoding it again
        # gives a Lua string literal that survives any payload content.
        lua_literal = json.dumps(payload)
        code = (
            'local payload = vim.json.decode('
            f"{lua_literal}"
            ")\n"
            'local ok, xli = pcall(require, "xli")\n'
            "if ok and type(xli.handle_rpc) == 'function' then\n"
            "  xli.handle_rpc(payload.event, payload.data)\n"
            "end"
        )
        return self.call_lua(code)

    # ---------------------------------------------------------------- inbound
    def handle_python_call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Dispatch a call coming from Lua. Returns None if unhandled."""
        handler = self._handlers.get(method)
        if handler is None:
            logger.log_structured("WARN", "xli.bridge", f"no handler for {method!r}")
            return None
        try:
            return handler(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - never propagate into the editor
            logger.log_error("xli.bridge", f"handler {method} failed", exc=exc)
            return None
