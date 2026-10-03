#!/usr/bin/env python3
"""Drive a full-screen program in a pty and print what its screen looks like.

Curses output is meaningless as a byte stream (escape codes, cursor moves,
partial repaints), so this runs the program in a pty, feeds it keystrokes, and
renders the result through pyte — a terminal emulator — which yields the
*text a human would see*, cell by cell.

Usage::

    python3 scripts/tui_probe.py --cols 100 --rows 30 -- python3 -m xli tui --provider fake
    python3 scripts/tui_probe.py --send "привет" --send "\r" --wait 2 -- <cmd>

Keys: ``\\r`` enter, ``\\x1b`` escape, ``\\x03`` ^C, ``\\x1b[A`` arrow up.
Everything else is sent literally. ``--send`` may be repeated; each one is
followed by the wait interval before the screen is captured. Pass ``--screen``
after every step (the default) to print all intermediate frames.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Runnable as `python3 scripts/tui_probe.py` from a checkout: the package sits
# one directory up and is not necessarily installed.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xli.tui.screen import ScreenSession, available, unavailable_reason  # noqa: E402


def run(argv: list[str], sends: list[str], waits: list[float], cols: int, rows: int):
    if not available():
        raise SystemExit(f"screen probing needs pyte: {unavailable_reason()}")
    frames: list[str] = []
    with ScreenSession(argv, cols=cols, rows=rows) as session:
        session.wait(waits[0] if waits else 1.5)
        frames.append(_frame(session))
        for index, keys in enumerate(sends):
            session.send(keys)
            session.wait(waits[index + 1] if index + 1 < len(waits) else 0.8)
            frames.append(_frame(session))
    return frames


def _frame(session: ScreenSession) -> str:
    lines = session.lines()
    return "\n".join("│" + line + "│" for line in lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cols", type=int, default=100)
    parser.add_argument("--rows", type=int, default=30)
    parser.add_argument("--send", action="append", default=[], help="keys to send (escapes honoured)")
    parser.add_argument(
        "--wait",
        action="append",
        type=float,
        default=[],
        help="seconds to wait before each capture",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("no command given (use `-- <command>`)")

    sends = [codecs_decode(item) for item in args.send]
    waits = args.wait or [1.5] + [0.8] * len(sends)

    frames = run(command, sends, waits, args.cols, args.rows)
    for index, frame in enumerate(frames):
        print(f"──── frame {index} ────")
        print(frame)
    return 0


def codecs_decode(text: str) -> str:
    """Turn `\\n`, `\\t`, `\\x1b` in a CLI argument into real keys.

    Latin-1 with backslash-replacement is the trick that keeps non-ASCII text
    intact while still expanding escape sequences: Cyrillic becomes `\\uXXXX`
    on the way in and the original character on the way out.
    """
    return text.encode("latin-1", "backslashreplace").decode("unicode_escape")


if __name__ == "__main__":
    sys.exit(main())
