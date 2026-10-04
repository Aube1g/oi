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
import time

from xli.mcp.transport import (
    HTTPTransport,
    SSETransport,
    StdioTransport,
    Transport,
    TransportError,
)

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
    """Launches and talks to the bundled and externally configured MCP servers."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self.timeout = timeout
        self._transports: dict[str, Transport] = {}
        self._next_id = 1
        logger.log_structured("INFO", "mcp.client", "Initialized")

    # ------------------------------------------------------------- transport
    def _server_info(self, server_name: str) -> dict:
        """The registry entry for a server, without importing it twice.

        A client built for a name that is not in the registry still works: the
        bundled-server convention (`python -m xli.mcp.servers.<name>`) is the
        fallback, which is what a caller who knows the name expects.
        """
        try:
            from xli.mcp.registry import get_registry

            return get_registry().get_server(server_name) or {}
        except Exception:  # noqa: BLE001 - the client must work without a registry
            return {}

    def _transport(self, server_name: str) -> Transport:
        transport = self._transports.get(server_name)
        if transport is not None:
            return transport

        info = self._server_info(server_name)
        timeout = float(info.get("tool_timeout") or self.timeout)
        startup = float(info.get("startup_timeout") or 20.0)
        env = dict(info.get("env") or {})
        url = str(info.get("url") or "")
        command = info.get("command")

        try:
            if url and str(info.get("transport") or "http") in ("http", "sse", "streamable-http"):
                transport = (
                    SSETransport(url, env=env, timeout=timeout, startup_timeout=startup)
                    if str(info.get("transport")) == "sse"
                    else HTTPTransport(url, env=env, timeout=timeout, startup_timeout=startup)
                )
            else:
                transport = StdioTransport(
                    list(command) if command else server_command(server_name),
                    env=env,
                    timeout=timeout,
                    startup_timeout=startup,
                )
        except OSError as exc:
            raise MCPError(f"could not start {server_name}: {exc}") from exc

        if info.get("enabled") is False:
            logger.log_structured(
                "WARN", "mcp.client", f"{server_name} is disabled in the config but was called anyway"
            )
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
            reply = transport.send(request)
        except TransportError as exc:
            raise MCPError(f"{server_name}: {exc}") from exc
        except (OSError, ValueError) as exc:
            raise MCPError(f"{server_name}: {exc}") from exc

        if not reply:
            raise MCPError(f"{server_name}: no response to {method}")

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
        tools = result.get("tools", []) or []
        info = self._server_info(server_name)
        if info.get("external") and (info.get("enabled_tools") or info.get("disabled_tools")):
            from xli.mcp.config import allowed_tools

            names = {tool.get("name") for tool in tools if isinstance(tool, dict)}
            keep = set(allowed_tools(info, list(names)))
            tools = [tool for tool in tools if isinstance(tool, dict) and tool.get("name") in keep]
        return tools

    def probe(self, server_name: str) -> dict:
        """Start a server, shake hands, and report what it is.

        This is the check a user needs before trusting a config file copied
        from somewhere: does the command exist, does it answer, and what does
        it call itself?
        """
        info = self._server_info(server_name)
        transport = self._transport(server_name)
        started = time.perf_counter()
        reply = transport.send(
            {
                "jsonrpc": "2.0",
                "id": "_probe",
                "method": "tools/list",
                "params": {},
            }
        )
        elapsed = time.perf_counter() - started

        if "error" in reply:
            error = reply["error"] or {}
            raise MCPError(f"{server_name}: {error.get('message', 'unknown error')}")

        result = reply.get("result") if isinstance(reply, dict) else None
        tools = (result or {}).get("tools", []) if isinstance(result, dict) else []
        names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
        if info.get("external") and (info.get("enabled_tools") or info.get("disabled_tools")):
            from xli.mcp.config import allowed_tools

            names = allowed_tools(info, names)
        return {
            "server": server_name,
            "transport": info.get("transport", "stdio"),
            "source": info.get("source", "bundled"),
            "seconds": round(elapsed, 3),
            "tools": names,
            "tool_count": len(names),
        }

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
