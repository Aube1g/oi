#!/usr/bin/env python3
"""
XLI Tool Registry — the single place tools are registered, looked up and run.

The registry owns three concerns that used to be tangled together:

  * **catalogue**  — what exists, enabled or not, grouped by tag.
  * **execution**  — run a tool by name, uniformly, timing it and converting
                     every failure mode into a ToolResult instead of an
                     exception.
  * **policy**     — ask the permission Policy before anything runs, so a
                     frontend that wants to prompt a human gets told to.

It deliberately does not know about the LLM. The agent asks the registry for a
prompt block, the model answers with `<tool>{...}</tool>`, the agent hands the
parsed call back to `execute()`.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from typing import Any
from collections.abc import Awaitable, Callable, Iterable

from xli.permissions.policy import MUTATING_TOOLS, Policy
from xli.tools.base import FunctionTool, Param, Tool, ToolError, ToolResult, ToolSpec


@dataclass
class RegistryEntry:
    tool: Tool
    enabled: bool = True
    calls: int = 0
    failures: int = 0
    total_ms: float = 0.0


class ToolRegistry:
    """Name -> Tool catalogue with policy-gated execution."""

    def __init__(self, policy: Policy | None = None):
        self._entries: dict[str, RegistryEntry] = {}
        self.policy = policy
        #: Called when the policy says a call needs a human yes/no. Return True
        #: to proceed. Frontends replace this to show a real prompt.
        #: Called when a tool needs confirmation. May be a plain callable
        #: returning bool, or a coroutine function — the latter is what an
        #: interactive frontend needs, since waiting for a human is async.
        self.confirm_handler: (
            Callable[[str, dict[str, Any], str], bool | Awaitable[bool]] | None
        ) = None

    # ------------------------------------------------------------ catalogue
    def register(
        self,
        tool_obj: Tool,
        *,
        enabled: bool = True,
        replace: bool = False,
    ) -> Tool:
        name = tool_obj.name
        if name in self._entries and not replace:
            raise ValueError(f"tool {name!r} is already registered (pass replace=True)")
        self._entries[name] = RegistryEntry(tool=tool_obj, enabled=enabled)
        return tool_obj

    def register_function(
        self,
        name: str,
        description: str,
        fn: Callable[..., Any],
        params: list[Param] | None = None,
        *,
        mutates: bool = False,
        tags: list[str] | None = None,
        enabled: bool = True,
        replace: bool = True,
    ) -> Tool:
        spec = ToolSpec(
            name=name,
            description=description,
            params=params or [],
            mutates=mutates,
            tags=tags or [],
        )
        return self.register(FunctionTool(spec, fn), enabled=enabled, replace=replace)

    def register_many(self, tools: Iterable[Tool], *, replace: bool = True) -> list[Tool]:
        return [self.register(t, replace=replace) for t in tools]

    def unregister(self, name: str) -> bool:
        return self._entries.pop(name, None) is not None

    def get(self, name: str) -> Tool | None:
        entry = self._entries.get(name)
        return entry.tool if entry else None

    def has(self, name: str) -> bool:
        return name in self._entries

    def is_enabled(self, name: str) -> bool:
        entry = self._entries.get(name)
        return bool(entry and entry.enabled)

    def set_enabled(self, name: str, enabled: bool) -> bool:
        entry = self._entries.get(name)
        if entry is None:
            return False
        entry.enabled = enabled
        return True

    def names(self, *, enabled_only: bool = True) -> list[str]:
        return sorted(
            name for name, entry in self._entries.items() if entry.enabled or not enabled_only
        )

    def all(self, *, enabled_only: bool = False) -> list[Tool]:
        return [
            entry.tool
            for _, entry in sorted(self._entries.items())
            if entry.enabled or not enabled_only
        ]

    def by_tag(self, tag: str, *, enabled_only: bool = True) -> list[Tool]:
        return [t for t in self.all(enabled_only=enabled_only) if tag in t.spec.tags]

    def tags(self) -> list[str]:
        found = {tag for entry in self._entries.values() for tag in entry.tool.spec.tags}
        return sorted(found)

    def stats(self) -> dict[str, Any]:
        return {
            name: {
                "enabled": entry.enabled,
                "calls": entry.calls,
                "failures": entry.failures,
                "avg_ms": round(entry.total_ms / entry.calls, 3) if entry.calls else 0.0,
                "mutates": entry.tool.spec.mutates,
            }
            for name, entry in sorted(self._entries.items())
        }

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, name: object) -> bool:
        return name in self._entries

    # ------------------------------------------------------------- rendering
    def schema(self, *, enabled_only: bool = True) -> list[dict[str, Any]]:
        return [t.spec.to_schema() for t in self.all(enabled_only=enabled_only)]

    def prompt_block(self, *, enabled_only: bool = True) -> str:
        """The tool list embedded in the system prompt, grouped by tag."""
        tools = self.all(enabled_only=enabled_only)
        if not tools:
            return ""
        grouped: dict[str, list[Tool]] = {}
        for t in tools:
            key = t.spec.tags[0] if t.spec.tags else "general"
            grouped.setdefault(key, []).append(t)

        lines: list[str] = []
        for tag in sorted(grouped):
            lines.append(f"[{tag}]")
            lines.extend(t.spec.to_prompt() for t in grouped[tag])
        lines.append("")
        lines.append("Call a tool with: <tool>{\"name\": \"<tool>\", \"args\": {...}}</tool>")
        return "\n".join(lines)

    # ------------------------------------------------------------ execution
    async def execute(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        args = dict(args or {})

        tool_obj = self.get(name)
        if tool_obj is None:
            return ToolResult.failure(
                f"unknown tool {name!r}; available: {', '.join(self.names())}", tool=name
            )

        entry = self._entries[name]
        if not entry.enabled:
            return ToolResult.failure(f"tool {name!r} is disabled", tool=name)

        if self.policy is not None:
            allowed = await self._authorise(name, tool_obj.spec, args)
            if allowed is not True:
                return allowed

        started = time.perf_counter()
        try:
            result = await tool_obj.run(**args)
        except ToolError as exc:
            result = ToolResult.failure(str(exc), tool=name)
        except TypeError as exc:
            # Almost always the model inventing an argument the tool lacks.
            result = ToolResult.failure(
                f"{name}: bad arguments ({exc}); expected {tool_obj.spec.required}", tool=name
            )
        except Exception as exc:  # noqa: BLE001 - registry boundary
            result = ToolResult.failure(f"{type(exc).__name__}: {exc}", tool=name)

        result.tool = result.tool or name
        if not result.duration_ms:
            result.duration_ms = (time.perf_counter() - started) * 1000

        entry.calls += 1
        entry.total_ms += result.duration_ms
        if not result.ok:
            entry.failures += 1
        return result

    async def _authorise(
        self, name: str, spec: ToolSpec, args: dict[str, Any]
    ) -> bool | ToolResult:
        """Returns True to proceed, or a ToolResult to short-circuit with.

        Async because a confirm handler may need to wait for a human. The
        synchronous version forced interactive frontends to block the running
        event loop, and the only way to do that from inside a coroutine is
        loop.run_until_complete(), which raises "This event loop is already
        running". The TUI hit exactly that. A handler may now be either a plain
        callable or a coroutine function.
        """
        decision = self.policy.check(name, args)
        if not decision.allowed:
            return ToolResult.failure(
                f"permission denied: {decision.reason}", tool=name, data=decision.to_dict()
            )

        if decision.needs_confirmation:
            if self.confirm_handler is None:
                return ToolResult.failure(
                    f"needs confirmation but no confirm handler is attached: {decision.reason}",
                    tool=name,
                    data=decision.to_dict(),
                )
            approved = self.confirm_handler(name, args, decision.reason)
            if inspect.isawaitable(approved):
                approved = await approved
            if not approved:
                return ToolResult.failure(
                    f"refused by user: {decision.reason}", tool=name, data=decision.to_dict()
                )
            self.policy.confirm(name, args)
        return True


# --------------------------------------------------------------------- presets
def mutating_names() -> tuple[str, ...]:
    """Tool names the Policy treats as mutating — kept here so they stay in sync."""
    return tuple(sorted(MUTATING_TOOLS))


def default_registry(policy: Policy | None = None) -> ToolRegistry:
    """A registry preloaded with the built-in toolset."""
    from xli.tools.builtin import builtin_tools

    registry = ToolRegistry(policy=policy)
    registry.register_many(builtin_tools())
    return registry
