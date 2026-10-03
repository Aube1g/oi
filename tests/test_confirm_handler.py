#!/usr/bin/env python3
"""Tests for confirmation handling in the tool registry.

The registry called confirm_handler synchronously, from _authorise, which runs
inside the async execute(). An interactive frontend therefore had to block the
event loop it was already running on, and the only way to do that from inside a
coroutine is loop.run_until_complete() — which raises
"This event loop is already running".

Reproduced against the real registry before the fix: a sync handler that pumped
the loop made execute() raise RuntimeError out of registry.py:226, so every
tool confirmation in the TUI failed.

_authorise is now async and a handler may be either a plain callable or a
coroutine function. Both are pinned here.
"""

import asyncio

import pytest

from xli.permissions.policy import Mode, Policy
from xli.tools.registry import default_registry


@pytest.fixture
def confirm_registry(tmp_path):
    """A registry in confirm mode, where mutating tools need a yes."""
    return default_registry(policy=Policy(mode=Mode.CONFIRM, root=tmp_path))


def run(coro):
    return asyncio.run(coro)


class TestAsyncConfirmHandler:
    def test_a_coroutine_handler_is_awaited(self, confirm_registry, tmp_path):
        """The regression: this used to raise 'event loop is already running'."""
        seen = []

        async def handler(tool, args, reason):
            seen.append((tool, reason))
            await asyncio.sleep(0)  # standing in for waiting on a human
            return True

        confirm_registry.confirm_handler = handler
        target = tmp_path / "made.txt"
        result = run(
            confirm_registry.execute("write", {"path": str(target), "content": "hi\n"})
        )

        assert result.ok, result.error
        assert target.read_text() == "hi\n"
        assert seen and seen[0][0] == "write"

    def test_a_coroutine_handler_can_refuse(self, confirm_registry, tmp_path):
        async def handler(tool, args, reason):
            await asyncio.sleep(0)
            return False

        confirm_registry.confirm_handler = handler
        target = tmp_path / "nope.txt"
        result = run(
            confirm_registry.execute("write", {"path": str(target), "content": "x"})
        )

        assert result.ok is False
        assert "refused by user" in (result.error or "")
        assert not target.exists()

    def test_a_handler_that_truly_waits_is_fine(self, confirm_registry, tmp_path):
        """Awaiting something real, not just yielding — the actual TUI case."""
        async def handler(tool, args, reason):
            await asyncio.sleep(0.02)
            return True

        confirm_registry.confirm_handler = handler
        result = run(
            confirm_registry.execute(
                "write", {"path": str(tmp_path / "a.txt"), "content": "x"}
            )
        )
        assert result.ok


class TestSyncConfirmHandler:
    """The CLI's shape must keep working — this is not an async-only API."""

    def test_a_plain_callable_is_still_accepted(self, confirm_registry, tmp_path):
        seen = []

        def handler(tool, args, reason):
            seen.append(tool)
            return True

        confirm_registry.confirm_handler = handler
        result = run(
            confirm_registry.execute(
                "write", {"path": str(tmp_path / "b.txt"), "content": "x"}
            )
        )

        assert result.ok, result.error
        assert seen == ["write"]

    def test_a_plain_callable_can_refuse(self, confirm_registry, tmp_path):
        confirm_registry.confirm_handler = lambda tool, args, reason: False
        result = run(
            confirm_registry.execute(
                "write", {"path": str(tmp_path / "c.txt"), "content": "x"}
            )
        )
        assert result.ok is False
        assert "refused by user" in (result.error or "")

    def test_the_cli_terminal_handler_shape_still_works(self, confirm_registry, tmp_path):
        """Mirrors xli.cli._terminal_confirm: sync, returns a bool."""
        from xli.cli import _terminal_confirm  # noqa: F401 - shape check

        confirm_registry.confirm_handler = lambda tool, args, reason: True
        assert run(
            confirm_registry.execute(
                "write", {"path": str(tmp_path / "d.txt"), "content": "x"}
            )
        ).ok


class TestNoHandlerAndNoConfirmation:
    def test_missing_handler_is_reported_not_raised(self, confirm_registry, tmp_path):
        confirm_registry.confirm_handler = None
        result = run(
            confirm_registry.execute(
                "write", {"path": str(tmp_path / "e.txt"), "content": "x"}
            )
        )
        assert result.ok is False
        assert "no confirm handler" in (result.error or "")

    def test_a_denied_tool_never_reaches_the_handler(self, tmp_path):
        called = []
        registry = default_registry(policy=Policy(mode=Mode.READONLY, root=tmp_path))
        registry.confirm_handler = lambda *a: called.append(a) or True

        result = run(
            registry.execute("write", {"path": str(tmp_path / "f.txt"), "content": "x"})
        )
        assert result.ok is False
        assert called == [], "a denied call must not prompt for confirmation"

    def test_auto_mode_does_not_confirm(self, tmp_path):
        called = []
        registry = default_registry(policy=Policy(mode=Mode.AUTO, root=tmp_path))
        registry.confirm_handler = lambda *a: called.append(a) or True

        result = run(
            registry.execute("write", {"path": str(tmp_path / "g.txt"), "content": "x"})
        )
        assert result.ok, result.error
        assert called == []

    def test_read_only_tools_are_not_confirmed(self, confirm_registry, tmp_path):
        (tmp_path / "h.txt").write_text("data\n", encoding="utf-8")
        called = []
        confirm_registry.confirm_handler = lambda *a: called.append(a) or True

        result = run(confirm_registry.execute("read", {"path": str(tmp_path / "h.txt")}))
        assert result.ok, result.error
        assert called == []


class TestAuthoriseIsAsync:
    def test_the_internal_gate_is_a_coroutine_function(self):
        import inspect

        from xli.tools.registry import ToolRegistry

        assert inspect.iscoroutinefunction(ToolRegistry._authorise)

    def _calls_named(self, name: str) -> list[str]:
        """Every real call to `name` under xli/, found via AST.

        A text grep is not enough: the docstrings that explain this very bug
        mention run_until_complete() and get_event_loop(), and matching prose
        would fail the test for documenting the fix. AST finds calls only.
        """
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "xli"
        found = []
        for path in root.rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                attr = func.attr if isinstance(func, ast.Attribute) else None
                if attr == name:
                    found.append(f"{path}:{node.lineno}")
        return found

    def test_no_frontend_blocks_a_running_loop(self):
        assert self._calls_named("run_until_complete") == [], (
            "run_until_complete() inside a running loop raises RuntimeError"
        )

    def test_no_deprecated_get_event_loop_calls(self):
        """Deprecated for the in-coroutine case; get_running_loop() is correct."""
        assert self._calls_named("get_event_loop") == []
