#!/usr/bin/env python3
"""The terminal questionnaire: `input()`, and nothing else.

This is the front end that has to work everywhere -- a bare install, a pipe, a
phone running Termux -- so it deliberately depends on no UI toolkit. The TUI and
Neovim variants are conveniences on top, not requirements.

`run` is synchronous. It used to be `async def` while doing nothing awaitable,
which meant every caller had to remember to await a function that never yielded
-- and the Neovim front end did not, so its fallback returned a coroutine object
instead of answers.
"""

from __future__ import annotations

from typing import TextIO
from collections.abc import Callable
import sys

from xli.ui.questionnaire.base import Question

#: Banner text, and the rule drawn under it.
#:
#: Deliberately ASCII. This writes to plain stdout, which may be a pipe or a
#: console that is not UTF-8 -- unlike the curses TUI, which negotiates a locale
#: before drawing. The previous version led with an emoji, and that was the
#: first thing to turn into mojibake.
BANNER = "xli - clarify the task"
RULE = "-" * 60


class TerminalQuestionnaire:
    """Asks questions on stdin and collects the answers.

    `reader` is injectable so the whole flow is testable without a pty: pass a
    callable that returns the next line and the questionnaire behaves exactly as
    it would for a person typing.
    """

    def __init__(
        self,
        *,
        reader: Callable[[str], str] | None = None,
        out: TextIO | None = None,
    ):
        self._reader = reader or input
        self._out = out if out is not None else sys.stdout

    # ------------------------------------------------------------------ output
    def _print(self, text: str = "") -> None:
        print(text, file=self._out)

    # --------------------------------------------------------------------- run
    def run(self, questions: list[Question]) -> dict[str, str]:
        """Ask every question, returning answers keyed by question id.

        A question the user skips is recorded as an empty string when it is
        optional, and re-asked when it is required. Every question ends up in
        the result, so a caller never has to guess whether a missing key means
        "not asked" or "asked and skipped".
        """
        if not questions:
            return {}

        self._print()
        self._print(RULE)
        self._print(f" {BANNER} ")
        self._print(RULE)

        answers: dict[str, str] = {}
        for question in questions:
            answers[question.id] = self._ask(question)

        self._print()
        self._print(f" {len(answers)} answer(s) recorded")
        self._print(RULE)
        return answers

    def _ask(self, question: Question) -> str:
        """Ask one question until the answer is acceptable, or give up cleanly."""
        self._print()
        self._print(f"  ? {question.prompt()}")
        if question.hint:
            self._print(f"    {question.hint}")
        if question.type == "choice":
            for line in question.numbered_options():
                self._print(f"    {line}")

        while True:
            try:
                raw = self._reader("    > ")
            except (EOFError, KeyboardInterrupt, RuntimeError, OSError):
                # No more input. `input()` raises EOFError at end of stream and
                # RuntimeError when sys.stdin is gone outright, so both are
                # caught: recording the default beats raising, because the
                # caller can still act on the answers already collected.
                self._print("    (input closed)")
                return question.default or ""

            value = question.normalise(raw)
            if question.is_acceptable(value):
                return value or ""

            if value is None and question.type == "choice":
                self._print(f"    pick one of 1-{len(question.options)}")
            elif question.type == "confirm":
                self._print("    answer y or n")
            else:
                self._print("    an answer is required")
