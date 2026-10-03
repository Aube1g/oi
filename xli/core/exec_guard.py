#!/usr/bin/env python3
"""
XLI Exec Guard — hardened subprocess.run(shell=True) wrapper.

This is NOT container isolation (no Docker/bubblewrap available in every
deployment target), but it's a real improvement over a bare subprocess.run:

  - CPU time and address-space limits via resource.setrlimit (Linux/macOS)
  - the child does not inherit API keys / tokens from the parent's env,
    so a compromised or hallucinated command can't exfiltrate them
  - always routed through shell_safety.is_shell_command_safe() first

True sandboxing (network namespaces, filesystem isolation) still needs an
external layer — see CHANGES.md for the recommended follow-up.
"""

import os
import subprocess

from xli.core.shell_safety import is_shell_command_safe
from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.exec_guard")

# Env var name fragments that should never reach a spawned shell command.
_SECRET_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY")


def _scrubbed_env() -> dict:
    env = os.environ.copy()
    for key in list(env.keys()):
        if any(marker in key.upper() for marker in _SECRET_MARKERS):
            del env[key]
    return env


def _limits(max_memory_mb: int, cpu_seconds: int):
    def _apply():
        try:
            import resource
            mem_bytes = max_memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        except Exception:
            pass  # best-effort; not available on all platforms (e.g. Windows)
    return _apply


def run_guarded_shell(
    command: str,
    timeout: int = 30,
    cwd: str | None = None,
    max_memory_mb: int = 512,
) -> subprocess.CompletedProcess:
    """Validate then run a shell command with resource limits and a scrubbed env.

    Raises ValueError if the command is blocked by shell_safety. Callers that
    want a soft error string instead of an exception should call
    is_shell_command_safe() themselves first (as BashTool/chain.py/env.py do)
    and only reach this function once a command has passed that check.
    """
    safe, reason = is_shell_command_safe(command)
    if not safe:
        raise ValueError(f"blocked: {reason}")

    preexec = _limits(max_memory_mb, timeout) if os.name != "nt" else None

    return subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        env=_scrubbed_env(),
        preexec_fn=preexec,
    )
