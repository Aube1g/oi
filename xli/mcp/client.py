#!/usr/bin/env python3
"""
XLI MCP client — talks to the bundled stdio MCP servers for real.

Each server under xli/mcp/servers/ is a standalone newline-delimited JSON-RPC
process. This client launches one per server on first use, keeps it warm, and
routes tool calls to it.

It replaces a placeholder that logged the call and returned
{"status": "ok", "result": f"MCP {server}.{tool} called"} without ever starting
a process, and whose list_tools() returned []. Every MCP call therefore
"succeeded" while doing nothing, which is worse than failing: the agent
received invented context and had no way to tell.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from xli.core.logger import StructuredLogger
from xli.mcp.transport import StdioTransport

logger = StructuredLogger("xli.mcp.client")

SERVERS_PACKAGE = "xli.mcp.servers"
DEFAULT_TIMEOUT = 30.0


class MCPError(Exception):
    """A server returned a JSON-RPC error, or could not be reached."""

    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


def server_command(name: str) -> list[str]:
    """How to launch a bundled server.

    The same interpreter that is running xli, so the server sees the same
    environment and the same installed dependencies.
    """
    return [sys.executable, "-m", f"{SERVERS_PACKAGE}.{name}"]


class MCPClient:
    """Launches and talks to the bundled stdio MCP servers."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self.timeout = timeout
        self._transports: dict[str, StdioTransport] = {}
        self._next_id = 1
        logger.log_structured("INFO", "mcp.client", "Initialized")

    # ------------------------------------------------------------- transport
    def _transport(self, server_name: str) -> StdioTransport:
        transport = self._transports.get(server_name)
        if transport is not None:
            return transport
        command = server_command(server_name)
        try:
            transport = StdioTransport(command)
        except OSError as exc:
            raise MCPError(f"could not start {server_name}: {exc}") from exc
        self._transports[server_name] = transport
        return transport

    # ------------------------------------------------------------------- rpc
    def _request(self, server_name: str, method: str, params: dict[str, Any]) -> Any:
        """One JSON-RPC round trip. Raises MCPError on any failure."""
        transport = self._transport(server_name)
        self._next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        try:
            raw = transport.send(request)
        except (OSError, ValueError) as exc:
            raise MCPError(f"{server_name}: {exc}") from exc

        if not raw or not raw.strip():
            raise MCPError(f"{server_name}: no response to {method}")

        try:
            reply = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MCPError(f"{server_name}: malformed response: {exc}") from exc

        if "error" in reply:
            err = reply["error"] or {}
            raise MCPError(
                f"{server_name}: {err.get('message', 'unknown error')}",
                code=err.get("code"),
            )
        return reply.get("result")

    # ------------------------------------------------------------------ api
    async def call_tool(self, server_name: str, tool: str, params: dict) -> Any:
        """Invoke one tool on one server and return its actual result."""
        logger.log_structured(
            "DEBUG", "mcp.client", f"Calling {server_name}.{tool}",
            {"params": str(params)[:100]},
        )
        result = self._request(
            server_name, "tools/call", {"name": tool, "arguments": params or {}}
        )
        return result

    async def list_tools(self, server_name: str) -> list:
        """The tools a server really offers, as reported by that server."""
        result = self._request(server_name, "tools/list", {})
        if not isinstance(result, dict):
            return []
        return result.get("tools", []) or []

    def close(self, server_name: str | None = None) -> None:
        """Shut down one server, or all of them."""
        targets = (
            list(self._transports.items())
            if server_name is None
            else [(server_name, self._transports[server_name])]
            if server_name in self._transports
            else []
        )
        for name, transport in targets:
            try:
                transport.close()
            except OSError as exc:  # noqa: PERF203 - closing must be best-effort
                logger.log_error("mcp.client", f"closing {name} failed", exc=exc)
            finally:
                self._transports.pop(name, None)

    def __enter__(self) -> MCPClient:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
