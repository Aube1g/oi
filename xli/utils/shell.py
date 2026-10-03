#!/usr/bin/env python3
"""
XLI Utils — SafeShell: validation and guarded execution.

This used to carry its own copy of a dangerous-command blocklist. That was the
fifth such list in the tree, and it had a real hole: the check was guarded by
`if "rm -rf" in cmd and "rm -rf ." not in cmd`, which *exempted* `rm -rf .` —
so "recursively delete the current directory" passed straight through, while
`rm -rf /tmp/foo` was rejected merely because it contains the substring
`rm -rf /`.

There is now one list, in `xli.core.shell_safety`, and one guarded runner, in
`xli.core.exec_guard` (which also scrubs secrets from the environment and
applies memory/CPU limits). Anything that wants to run a shell command goes
through those two, so the safety rules cannot drift between call sites.
"""

from __future__ import annotations

import subprocess

from xli.core.exec_guard import run_guarded_shell
from xli.core.logger import StructuredLogger
from xli.core.shell_safety import is_shell_command_safe

logger = StructuredLogger("xli.utils.shell")


class SafeShell:
    """Safe shell execution. Thin facade over the shared guard."""

    @staticmethod
    def is_dangerous(cmd: str) -> str | None:
        """Return why a command is dangerous, or None if it looks fine.

        Kept as a predicate rather than a raise, so callers can turn the
        verdict into a tool result or a confirmation prompt instead of an
        exception.
        """
        safe, reason = is_shell_command_safe(cmd)
        return None if safe else reason

    @staticmethod
    def run(
        cmd: str,
        timeout: int = 30,
        cwd: str | None = None,
        capture: bool = True,
        max_memory_mb: int = 512,
    ) -> subprocess.CompletedProcess:
        """Validate, then run with a scrubbed env and resource limits.

        Raises ValueError if the command is blocked. `capture=False` is not
        supported by the guarded runner — everything is captured so secrets
        cannot leak to the parent's streams — so it is accepted only for
        signature compatibility and ignored.
        """
        danger = SafeShell.is_dangerous(cmd)
        if danger:
            logger.log_structured("WARN", "shell", f"BLOCKED: {danger}")
            raise ValueError(f"blocked: {danger}")

        logger.log_structured("INFO", "shell", f"executing: {cmd[:80]}")
        return run_guarded_shell(cmd, timeout=timeout, cwd=cwd, max_memory_mb=max_memory_mb)
