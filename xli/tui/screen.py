#!/usr/bin/env python3
"""Drive a full-screen program in a pty and read back what it painted.

Curses output is meaningless as a byte stream — escape codes, cursor moves,
partial repaints. This module runs the program in a pseudo-terminal, feeds it
keystrokes, and renders the bytes through :mod:`pyte`, a terminal emulator,
which yields the *text a human would see*, cell by cell.

It exists because "the TUI works" was, for a long time, an unverified claim.
Unit tests covered the row builders; nothing covered the thing itself: that
`xli tui` starts, paints a frame, accepts a line, runs the agent, and draws the
answer — with a real terminal, a real event loop and a real curses.

``pyte`` is an optional dependency. When it is missing, :func:`available`
returns False and callers skip; the suite must not fail because a test-only
library is absent.
"""

from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import struct
import termios
import time

try:  # pragma: no cover - exercised by the environment, not by logic
    import pyte

    _PYTE_ERROR: str | None = None
except Exception as exc:  # noqa: BLE001 - report the reason, do not crash
    pyte = None
    _PYTE_ERROR = str(exc)


def available() -> bool:
    """Is the terminal emulator importable?"""
    return pyte is not None


def unavailable_reason() -> str:
    return _PYTE_ERROR or "pyte is not installed"


class ScreenSession:
    """One program, one pty, one emulated screen."""

    def __init__(
        self,
        argv: list[str],
        *,
        cols: int = 100,
        rows: int = 30,
        env: dict[str, str] | None = None,
    ):
        if pyte is None:
            raise RuntimeError(unavailable_reason())
        self.argv = argv
        self.cols = cols
        self.rows = rows
        self.env = env or {}
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # child
            environment = os.environ.copy()
            environment.update(self.env)
            environment["TERM"] = environment.get("TERM", "xterm-256color")
            environment.setdefault("LANG", "C.UTF-8")
            environment.setdefault("LC_ALL", "C.UTF-8")
            environment["COLUMNS"] = str(cols)
            environment["LINES"] = str(rows)
            try:
                os.execvpe(argv[0], argv, environment)
            except OSError as exc:  # pragma: no cover - only on a bad command
                os.write(2, f"exec failed: {exc}\n".encode())
                os._exit(127)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)

    # ------------------------------------------------------------------ input
    def send(self, keys: str | bytes) -> None:
        """Send keystrokes. `\\r` is Enter, `\\x1b` Escape, `\\t` Tab."""
        payload = keys if isinstance(keys, bytes) else keys.encode("utf-8")
        os.write(self.fd, payload)

    def wait(self, seconds: float = 0.6) -> "ScreenSession":
        """Read whatever the program paints for `seconds`."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            ready, _, _ = select.select([self.fd], [], [], max(0.0, deadline - time.time()))
            if not ready:
                continue
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            self.stream.feed(chunk)
        return self

    # ----------------------------------------------------------------- output
    def lines(self) -> list[str]:
        """The screen as text, trailing blank lines removed."""
        out = [line.rstrip() for line in self.screen.display]
        while out and not out[-1]:
            out.pop()
        return out

    def text(self) -> str:
        return "\n".join(self.lines())

    def contains(self, needle: str) -> bool:
        return needle in self.text()

    def row(self, index: int) -> str:
        lines = self.lines()
        return lines[index] if 0 <= index < len(lines) else ""

    def close(self) -> None:
        try:
            os.kill(self.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass

    def __enter__(self) -> "ScreenSession":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


__all__ = ["ScreenSession", "available", "unavailable_reason"]
