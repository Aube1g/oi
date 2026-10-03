#!/usr/bin/env python3
"""
XLI Tools — base types for the tool layer.

A tool is three things bundled together:

  * a **spec**    — name, description, JSON schema, and whether it mutates.
                    This is what the LLM sees and what `xli tools schema` prints.
  * a **run()**   — the implementation. Sync or async, either is fine; the
                    registry awaits whichever you give it.
  * a **result**  — structured output plus a human-readable summary, so a
                    frontend can render either without re-parsing.

Tools never decide whether they are allowed to run — that is the Policy's job
(see xli.permissions). They also never print. This keeps them reusable from the
CLI, the TUI, the Neovim bridge and the JSON-RPC kernel alike.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable


class ToolError(Exception):
    """Raised by a tool when the call is invalid or cannot be completed.

    Surfaces to the LLM as a normal (non-fatal) tool error so it can retry with
    corrected arguments, instead of killing the agent loop.
    """


@dataclass(slots=True)
class Param:
    """One entry of a tool's argument schema."""

    name: str
    type: str = "string"
    description: str = ""
    required: bool = False
    enum: list[str] | None = None
    default: Any = None

    def to_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": self.type}
        if self.description:
            schema["description"] = self.description
        if self.enum:
            schema["enum"] = self.enum
        if self.default is not None:
            schema["default"] = self.default
        return schema


@dataclass(slots=True)
class ToolSpec:
    """Machine-readable description of a tool, suitable for an LLM prompt."""

    name: str
    description: str
    params: list[Param] = field(default_factory=list)
    mutates: bool = False
    tags: list[str] = field(default_factory=list)

    @property
    def required(self) -> list[str]:
        return [p.name for p in self.params if p.required]

    def to_schema(self) -> dict[str, Any]:
        properties = {p.name: p.to_schema() for p in self.params}
        return {
            "name": self.name,
            "description": self.description,
            "mutates": self.mutates,
            "tags": self.tags,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": self.required,
            },
        }

    def to_prompt(self) -> str:
        """Compact one-block rendering used inside the system prompt."""
        args = ", ".join(
            f"{p.name}{'*' if p.required else ''}:{p.type}" for p in self.params
        )
        return f"- {self.name}({args}) — {self.description}"


@dataclass(slots=True)
class ToolResult:
    """Outcome of a tool call."""

    ok: bool
    data: Any = None
    summary: str = ""
    error: str | None = None
    duration_ms: float = 0.0
    tool: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tool": self.tool,
            "data": self.data,
            "summary": self.summary,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 3),
        }

    @classmethod
    def success(cls, data: Any = None, summary: str = "", tool: str = "") -> ToolResult:
        return cls(ok=True, data=data, summary=summary or _summarize(data), tool=tool)

    @classmethod
    def failure(cls, error: str, tool: str = "", data: Any = None) -> ToolResult:
        return cls(ok=False, error=error, summary=f"error: {error}", data=data, tool=tool)

    def render(self) -> str:
        """Single-string form for terminals that only have one line to spare."""
        return self.summary if self.ok else f"error: {self.error}"


def _summarize(data: Any) -> str:
    if data is None:
        return "ok"
    if isinstance(data, str):
        return data if len(data) <= 200 else data[:197] + "..."
    if isinstance(data, (list, tuple)):
        return f"{len(data)} item(s)"
    if isinstance(data, dict):
        return ", ".join(f"{k}={v}" for k, v in list(data.items())[:4])
    return str(data)


class Tool(ABC):
    """Base class for tools implemented as classes."""

    #: overridden by subclasses
    spec: ToolSpec

    @abstractmethod
    async def run(self, **kwargs: Any) -> ToolResult:
        """Execute the tool. Must not raise for ordinary misuse — return failure()."""

    @property
    def name(self) -> str:
        return self.spec.name

    def validate(self, args: dict[str, Any]) -> str | None:
        """Return an error string if required params are missing, else None."""
        missing = [name for name in self.spec.required if name not in args or args[name] is None]
        if missing:
            return f"{self.name}: missing required argument(s): {', '.join(missing)}"
        return None


class FunctionTool(Tool):
    """Adapts a plain function into a Tool.

    The function may be sync or async and may return either a ToolResult or any
    JSON-able value — a bare value is wrapped into a successful result. Raising
    ToolError becomes a clean failure result; any other exception is captured
    too, because a tool crash must never take the agent loop down with it.
    """

    def __init__(self, spec: ToolSpec, fn: Callable[..., Any]):
        self.spec = spec
        self._fn = fn

    @property
    def name(self) -> str:
        return self.spec.name

    async def run(self, **kwargs: Any) -> ToolResult:
        problem = self.validate(kwargs)
        if problem:
            return ToolResult.failure(problem, tool=self.name)

        started = time.perf_counter()
        try:
            outcome = self._fn(**kwargs)
            if inspect.isawaitable(outcome):
                outcome = await outcome
        except ToolError as exc:
            return self._timed(ToolResult.failure(str(exc), tool=self.name), started)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - tool boundary
            return self._timed(
                ToolResult.failure(f"{type(exc).__name__}: {exc}", tool=self.name), started
            )

        if isinstance(outcome, ToolResult):
            outcome.tool = outcome.tool or self.name
            return self._timed(outcome, started)
        return self._timed(ToolResult.success(outcome, tool=self.name), started)

    @staticmethod
    def _timed(result: ToolResult, started: float) -> ToolResult:
        result.duration_ms = (time.perf_counter() - started) * 1000
        return result


def tool(
    name: str,
    description: str,
    params: list[Param] | None = None,
    *,
    mutates: bool = False,
    tags: list[str] | None = None,
) -> Callable[[Callable[..., Any]], FunctionTool]:
    """Decorator turning a function into a FunctionTool with a spec."""

    def decorator(fn: Callable[..., Any]) -> FunctionTool:
        spec = ToolSpec(
            name=name,
            description=description,
            params=params or [],
            mutates=mutates,
            tags=tags or [],
        )
        tool_obj = FunctionTool(spec, fn)
        # Keep the original reachable for tests and for direct in-process calls.
        tool_obj.fn = fn  # type: ignore[attr-defined]
        return tool_obj

    return decorator
