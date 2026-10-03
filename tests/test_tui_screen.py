#!/usr/bin/env python3
"""The TUI, verified on a real terminal.

These tests start `xli tui` inside a pseudo-terminal, type into it, and assert
what a human would see on the screen — not what a row builder returns. That
distinction matters: every layout bug this interface has had (a missing right
border, an input line overwritten by the status bar, mojibake instead of
Cyrillic, a frozen screen during a tool call) lived in the gap between
"the widgets are correct" and "the terminal shows it".

`pyte` renders the pty byte stream into a screen grid. It is an optional
dependency: when it is missing the whole module skips rather than failing, so
a lean environment still runs the rest of the suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from xli.tui import screen as screen_mod

pytestmark = pytest.mark.skipif(
    not screen_mod.available(),
    reason=f"terminal emulation unavailable: {screen_mod.unavailable_reason()}",
)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def tui():
    """A running TUI on the offline provider, closed when the test ends."""
    sessions = []

    def start(*, cols: int = 96, rows: int = 26, args: list[str] | None = None):
        argv = [sys.executable, "-m", "xli", "tui", "--provider", "fake", *(args or [])]
        session = screen_mod.ScreenSession(argv, cols=cols, rows=rows)
        sessions.append(session)
        session.wait(2.5)
        return session

    yield start
    for session in sessions:
        session.close()


class TestFrame:
    def test_paints_the_frame(self, tui):
        text = tui().text()
        assert text.count("╭") == 1, "the frame must have exactly one top-left corner"
        assert any(line.startswith("│") for line in text.splitlines())

    def test_header_carries_identity_and_mode(self, tui):
        text = tui().text()
        assert "XLI" in text
        assert "fake" in text and "mistral" in text
        assert "confirm" in text

    def test_every_line_is_the_same_width(self, tui):
        """A ragged right edge is the first thing the eye catches.

        The rows are built one cell short of the terminal, so the emulated
        screen pads them; what must hold is that the *content* lines all close
        with their border at the same column.
        """
        lines = [line for line in tui().lines() if line]
        widths = {len(line) for line in lines}
        assert max(widths) - min(widths) <= 1, f"ragged frame: {sorted(widths)}"

    def test_input_line_is_present_and_empty_at_start(self, tui):
        text = tui().text()
        assert "❯" in text
        assert "/help" in text, "the hint tells the user where to start"


class TestTyping:
    def test_cyrillic_survives_the_terminal(self, tui):
        session = tui()
        session.send("привет\n")
        session.wait(1.5)
        assert "вы привет" in session.text()

    def test_the_agent_answers_on_screen(self, tui):
        session = tui()
        session.send("скажи что-нибудь\n")
        session.wait(2.0)
        text = session.text()
        assert "◆ xli" in text, "the assistant badge must be visible"
        assert "OK" in text, "the fake provider's answer must be shown"

    def test_backspace_edits_the_line(self, tui):
        session = tui()
        session.send("приветX")
        session.wait(0.5)
        session.send("\x7f")
        session.wait(0.5)
        assert "привет" in session.text()
        assert "приветX" not in session.text()

    def test_input_history_with_the_up_key(self, tui):
        session = tui()
        session.send("первая задача\n")
        session.wait(1.2)
        session.send("\x1b[A")  # Up
        session.wait(0.5)
        # The prompt line shows the recalled text.
        prompt_line = next(line for line in session.lines() if "❯" in line)
        assert "первая задача" in prompt_line


class TestPanels:
    def test_tab_opens_the_plan_panel(self, tui):
        session = tui()
        session.send("\t")
        session.wait(1.0)
        text = session.text()
        assert "план" in text and "статистика" in text, "the tab strip must list the panes"
        assert "задач пока нет" in text or "прогресс" in text

    def test_tab_cycles_to_the_graph_panel(self, tui):
        session = tui()
        session.send("\t")
        session.wait(0.8)
        session.send("\t")
        session.wait(3.0)  # the graph scan runs in a worker thread
        text = session.text()
        assert "граф" in text
        assert "циклы импорта" in text or "считаю" in text

    def test_escape_closes_the_panel(self, tui):
        session = tui()
        session.send("\t")
        session.wait(0.8)
        session.send("\x1b")
        session.wait(0.8)
        assert "статистика" not in session.text()
        assert "❯" in session.text(), "the input line is back"

    def test_slash_help_switches_the_body(self, tui):
        session = tui()
        session.send("/help\n")
        session.wait(1.0)
        text = session.text()
        assert "клавиши" in text and "slash-команды" in text


class TestStatusBar:
    def test_status_shows_mode_and_keys(self, tui):
        text = tui().text()
        assert "готов" in text
        assert "^C" in text and "выход" in text

    def test_quit_key_exits_cleanly(self, tui):
        """^C leaves the program, with the terminal state restored.

        In a pty ^C is a signal to the foreground process group, not bytes on
        the wire — and inside a running asyncio loop that signal used to be
        swallowed, leaving a live process with a dead interface.
        """
        import os

        session = tui()
        session.send("\x03")
        session.wait(1.5)
        pid, status = os.waitpid(session.pid, os.WNOHANG)
        assert pid, "the TUI must exit on ^C"
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 130
