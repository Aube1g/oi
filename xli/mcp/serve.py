#!/usr/bin/env python3
"""MCP server mode — XLI's tools, offered to other agents.

XLI is an MCP *client* everywhere else in this package: it runs other people's
servers. This module is the other direction. `xli mcp serve` speaks the Model
Context Protocol on stdin/stdout, so Claude Code, Codex, Neovim or anything
else that hosts MCP servers can call XLI's tools — `read`, `edit`, `grep`,
`bash`, `dep_graph`, `chart`, `repo_map`, all 23 of them — with the schemas
they already know how to consume.

Why this is worth having: those harnesses have their own loop and their own
model, but they do not have XLI's tools. A Claude Code user who adds this
server gets terminal-native charts, the dependency graph, the repo map and the
sub-agent delegator inside their own editor, and the code that executes them is
the same code XLI runs — not a reimplementation that drifts.

Two rules the protocol cares about:

* **stdout is protocol only.** Every byte printed there must be a JSON-RPC
  frame. `xli`'s CLI prints Russian banners and the logger can write to the
  console, so the serving function redirects stdout while it builds the
  registry and restores it before the loop starts.
* **a bad tool call is an error result, not a dead server.** A tool that
  raises, or an unknown tool name, comes back as `isError: true` with the
  message; the loop keeps reading.

`--with-agent` additionally exposes `xli_agent`, which hands a whole task to
the XLI agent loop. It is off by default on purpose: it costs a second model
call per invocation, and a harness that decided to "delegate everything" would
be paying twice for every step.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from xli.mcp.serverkit import PROTOCOL_VERSION, shake_hands

#: Tools that mutate the workspace are still offered — refusing to expose them
#: would make the server useless for actual work — but they go through the same
#: permission policy as any other XLI call, so a `readonly` policy still says no.
DEFAULT_ROOT = Path.cwd()


def _version() -> str:
    try:
        from xli.version import VERSION

        return str(VERSION)
    except Exception:  # noqa: BLE001 - the version is cosmetic here
        return "unknown"


def _text_of(result: Any) -> str:
    """The most useful single string for a tool result.

    Tools that render something (charts, the dependency graph, the repo map)
    put the drawing in `data["text"]` and a one-line count in `summary`; tools
    that return data return dicts. The caller is a language model, so it gets
    the drawing when there is one and the JSON otherwise. A failed call is a
    different question — there the *error* is the useful string, and an empty
    content block would tell the caller nothing at all.
    """
    if not result.get("ok", True):
        return str(result.get("error") or result.get("summary") or "вызов не удался")
    data = result.get("data")
    if isinstance(data, dict) and isinstance(data.get("text"), str) and data["text"]:
        return data["text"]
    if isinstance(data, str) and data:
        return data
    summary = result.get("summary") or ""
    if data is None:
        return summary
    try:
        return f"{summary}\n{json.dumps(data, ensure_ascii=False, indent=2, default=str)}"
    except (TypeError, ValueError):
        return summary or str(data)


class XliMcpServer:
    """XLI's tool registry, addressed over MCP."""

    def __init__(
        self,
        *,
        registry: Any = None,
        with_agent: bool = False,
        tools: list[str] | None = None,
        root: Path | None = None,
    ) -> None:
        self.root = Path(root or DEFAULT_ROOT).expanduser()
        self.registry = registry if registry is not None else self._build_registry()
        self.with_agent = bool(with_agent)
        self._wanted = {name.strip() for name in (tools or []) if name.strip()}

    # ------------------------------------------------------------- registry
    def _build_registry(self):
        """Build the tool registry with cwd as the project root."""
        previous = Path.cwd()
        try:
            # Tools resolve relative paths against the process cwd, so the
            # project root has to be the cwd for the duration of the build.
            import os

            os.chdir(self.root)
            from xli.tools.registry import default_registry

            return default_registry()
        finally:
            with contextlib.suppress(OSError):
                import os

                os.chdir(previous)

    def tool_names(self) -> list[str]:
        names = list(self.registry.names())
        if self._wanted:
            names = [name for name in names if name in self._wanted]
        if self.with_agent:
            names.append("xli_agent")
        return names

    def list_tools(self) -> list[dict[str, Any]]:
        """tools/list: the registry's own schemas, plus the optional agent tool."""
        catalogue = {spec["name"]: spec for spec in self.registry.schema()}
        tools: list[dict[str, Any]] = []
        for name in self.tool_names():
            if name == "xli_agent":
                tools.append(
                    {
                        "name": "xli_agent",
                        "description": (
                            "Hand a whole task to the XLI agent: it plans, calls tools and "
                            "edits files on its own, then returns a summary. Use it for "
                            "multi-step work; use the individual tools for single actions."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "task": {"type": "string", "description": "What to do"},
                                "max_steps": {
                                    "type": "integer",
                                    "description": "Step budget (default: the configured limit)",
                                },
                            },
                            "required": ["task"],
                        },
                    }
                )
                continue
            spec = catalogue.get(name)
            if spec is None:
                continue
            tools.append(
                {
                    "name": name,
                    "description": spec.get("description") or "",
                    # The registry already publishes JSON Schema; MCP wants the
                    # same shape under a different key.
                    "inputSchema": spec.get("parameters") or {"type": "object", "properties": {}},
                    "annotations": {"readOnlyHint": not bool(spec.get("mutates"))},
                }
            )
        return tools

    # ---------------------------------------------------------------- calling
    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """tools/call: run one tool and translate the result into MCP content."""
        arguments = dict(arguments or {})
        if name == "xli_agent":
            return self._call_agent(arguments)

        if name not in self.tool_names():
            return {
                "content": [
                    {"type": "text", "text": f"unknown tool {name!r}; available: "
                                             + ", ".join(self.tool_names())}
                ],
                "isError": True,
            }

        try:
            result = asyncio.run(self.registry.execute(name, arguments))
        except Exception as exc:  # noqa: BLE001 - a failed tool is a result
            return {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            }

        payload = result.to_dict()
        text = _text_of(payload)
        content: dict[str, Any] = {
            "content": [{"type": "text", "text": text}],
            "isError": not payload.get("ok", False),
        }
        # Structured data, for callers that want to do something with it.
        if isinstance(payload.get("data"), (dict, list)) and "text" not in (payload.get("data") or {}):
            content["structuredContent"] = payload["data"]
        return content

    def _call_agent(self, arguments: dict[str, Any]) -> dict[str, Any]:
        task = str(arguments.get("task") or "").strip()
        if not task:
            return {
                "content": [{"type": "text", "text": "task is required"}],
                "isError": True,
            }

        async def run() -> dict[str, Any]:
            from xli.kernel.methods import build_kernel

            kernel = build_kernel()
            reply = await kernel.feed(
                _request_obj("agent.run", {"task": task, **(
                    {"max_steps": int(arguments["max_steps"])} if arguments.get("max_steps") else {}
                )})
            )
            return reply.to_dict().get("result") or {}

        try:
            result = asyncio.run(run())
        except Exception as exc:  # noqa: BLE001
            return {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            }

        ok = bool(result.get("ok"))
        lines = [result.get("summary") or ("готово" if ok else "не удалось")]
        if result.get("text"):
            lines.append(str(result["text"]))
        lines.append(
            "шагов: {steps}, вызовов: {calls}, ошибок: {errors}".format(
                steps=result.get("steps", 0),
                calls=result.get("tool_calls", 0),
                errors=result.get("tool_errors", 0),
            )
        )
        return {"content": [{"type": "text", "text": "\n".join(lines)}], "isError": not ok}

    # ---------------------------------------------------------------- protocol
    def handle_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """One JSON-RPC frame in, zero or one frame out."""
        handshake = shake_hands(request)
        if handshake is not None and request.get("method") != "tools/list":
            return handshake if request.get("id") is not None else None

        method = request.get("method")
        params = request.get("params") or {}
        request_id = request.get("id")

        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": self.list_tools()}}

        if method == "tools/call":
            name = str(params.get("name") or "")
            result = self.call(name, params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": request_id, "result": result}

        if method == "prompts/list":
            # Skills as prompts: `/xli <skill>` is how a Claude Code user would
            # reach them, and the list is cheap to produce.
            return {"jsonrpc": "2.0", "id": request_id, "result": {"prompts": self._prompts()}}

        if method == "prompts/get":
            prompt = self._prompt_text(str(params.get("name") or ""))
            if prompt is None:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": f"no such prompt: {params.get('name')}"},
                }
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "description": "XLI skill",
                    "messages": [
                        {"role": "user", "content": {"type": "text", "text": prompt}}
                    ],
                },
            }

        if method in ("resources/list", "resources/templates/list"):
            return {"jsonrpc": "2.0", "id": request_id, "result": {"resources": []}}

        if isinstance(method, str) and method.startswith("notifications/"):
            return None

        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }

    # ---------------------------------------------------------------- prompts
    def _skills(self) -> list[tuple[str, str]]:
        try:
            from xli.core.skills import SkillLibrary

            library = SkillLibrary()
            found = library.search("", limit=500) if hasattr(library, "search") else []
        except Exception:  # noqa: BLE001 - prompts are a bonus
            return []
        names: list[tuple[str, str]] = []
        for skill in found or []:
            name = getattr(skill, "name", None) or (skill.get("name") if isinstance(skill, dict) else None)
            if name:
                names.append((str(name), str(getattr(skill, "path", "") or "")))
        return names

    def _prompts(self) -> list[dict[str, str]]:
        return [
            {"name": name, "description": "Скилл XLI: " + name} for name, _ in self._skills()[:200]
        ]

    def _prompt_text(self, name: str) -> str | None:
        for skill_name, path in self._skills():
            if skill_name != name:
                continue
            try:
                return Path(path).read_text(encoding="utf-8")
            except OSError:
                return None
        return None

    # ------------------------------------------------------------------- loop
    def serve(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
        """Read frames until EOF. Errors are reported, never fatal."""
        stream_in = stdin or sys.stdin
        stream_out = stdout or sys.stdout

        for line in stream_in:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except ValueError as exc:
                _write(stream_out, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32700, "message": f"parse error: {exc}"}})
                continue

            try:
                reply = self.handle_request(request)
            except Exception as exc:  # noqa: BLE001 - one bad call must not stop the server
                reply = {"jsonrpc": "2.0", "id": request.get("id"),
                         "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}}
            if reply is not None:
                _write(stream_out, reply)
        return 0


def _write(stream: TextIO, frame: dict[str, Any]) -> None:
    stream.write(json.dumps(frame, ensure_ascii=False) + "\n")
    stream.flush()


def _request_obj(method: str, params: dict[str, Any]):
    from xli.kernel.protocol import Request

    return Request(id=1, method=method, params=params)


def main(argv: list[str] | None = None) -> int:
    """`xli mcp serve` — keep stdout clean while the registry is built."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="xli mcp serve", description="Offer XLI's tools over MCP (stdio)"
    )
    parser.add_argument("--with-agent", action="store_true",
                        help="also expose xli_agent, which runs the full agent loop")
    parser.add_argument("--tools", default="", help="comma-separated allow-list of tools")
    parser.add_argument("--root", default="", help="project root (default: cwd)")
    parser.add_argument("--protocol-version", default=PROTOCOL_VERSION)
    args = parser.parse_args(argv)

    # Anything printed while the registry loads — banners, warnings, the
    # logger — would corrupt the protocol stream. Send it to stderr.
    with contextlib.redirect_stdout(sys.stderr):
        server = XliMcpServer(
            with_agent=args.with_agent,
            tools=[name for name in args.tools.split(",") if name.strip()],
            root=Path(args.root) if args.root else None,
        )

    return server.serve()


if __name__ == "__main__":
    raise SystemExit(main())
