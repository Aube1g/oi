#!/usr/bin/env python3
"""The UI port: what a front end must be able to do.

Several parts of xli need to show something or ask something without knowing
whether they are running in a terminal, the curses TUI or Neovim. This module is
the contract between them.

It replaces an `XliUIAdapter` whose methods looked like a UI and were not one:
`confirm()` returned `True` without asking, `choice()` returned the default
without asking, and `input()` returned the default without asking. Wired into a
permission prompt, that is unconditional auto-approval of every tool call --
which is worse than no adapter at all, because it looks like the user agreed.

So the rule here is explicit: a front end that cannot ask must say so. The base
class raises rather than guessing, and `can_ask()` lets a caller choose a
fallback instead of receiving a fabricated yes.
"""

from __future__ import annotations

from collections.abc import Sequence

from xli.ui.questionnaire.base import Question


class UiPort:
    """What a front end must implement.

    Every asking method raises `NotImplementedError` by default. That is
    deliberate: a silent default answer to a question the user was never shown
    is how a tool gets permission nobody gave.
    """

    #: Set by a concrete front end that can actually put a question to a human.
    can_ask: bool = False

    # ------------------------------------------------------------------ output
    def display(self, text: str, *, title: str | None = None) -> None:
        """Show text. Never needs to ask, so it has a safe no-op default."""

    def notify(self, message: str, *, level: str = "info") -> None:
        """Show a short message. Also safe as a no-op."""

    def progress(self, percent: int, message: str = "") -> None:
        """Report progress. Safe as a no-op."""

    def clear(self) -> None:
        """Clear the working area. Safe as a no-op."""

    # ----------------------------------------------------------------- asking
    def ask(self, questions: Sequence[Question]) -> dict[str, str]:
        """Ask a list of questions, returning answers keyed by question id."""
        raise NotImplementedError(f"{type(self).__name__} cannot ask questions")

    def input(self, prompt: str, default: str = "") -> str:
        """Ask one free-text question."""
        raise NotImplementedError(f"{type(self).__name__} cannot ask questions")

    def choice(self, prompt: str, options: Sequence[str], default: int = 0) -> int:
        """Ask one single-select question, returning the chosen index."""
        raise NotImplementedError(f"{type(self).__name__} cannot ask questions")

    def confirm(self, prompt: str) -> bool:
        """Ask one yes/no question.

        Must not default to True. A front end that cannot ask should raise, so
        the caller falls back to a mode that does not need permission at all.
        """
        raise NotImplementedError(f"{type(self).__name__} cannot ask questions")


class TerminalUi(UiPort):
    """The terminal front end.

    Output goes to stdout; asking goes through the questionnaire, so the two
    cannot drift apart in how a question is phrased or validated.
    """

    can_ask = True

    def __init__(self, out=None):
        import sys

        self._out = out if out is not None else sys.stdout

    def display(self, text: str, *, title: str | None = None) -> None:
        if title:
            print(f"\n{title}\n{'─' * len(title)}", file=self._out)
        print(text, file=self._out)

    def notify(self, message: str, *, level: str = "info") -> None:
        print(f"[{level}] {message}", file=self._out)

    def progress(self, percent: int, message: str = "") -> None:
        print(f"\r{message} {percent}%".rstrip(), end="", file=self._out, flush=True)

    def clear(self) -> None:
        print("\033[2J\033[H", end="", file=self._out, flush=True)

    def ask(self, questions: Sequence[Question]) -> dict[str, str]:
        from xli.ui.questionnaire.terminal import TerminalQuestionnaire

        return TerminalQuestionnaire(out=self._out).run(list(questions))

    def input(self, prompt: str, default: str = "") -> str:
        from xli.ui.questionnaire.base import Question

        answers = self.ask([Question(id="answer", question=prompt, default=default)])
        return answers.get("answer", default)

    def choice(self, prompt: str, options: Sequence[str], default: int = 0) -> int:
        from xli.ui.questionnaire.base import Question

        options = list(options)
        if not options:
            raise ValueError("choice() needs at least one option")
        default_text = options[default] if 0 <= default < len(options) else None
        answers = self.ask(
            [
                Question(
                    id="answer",
                    question=prompt,
                    type="choice",
                    options=options,
                    default=default_text,
                )
            ]
        )
        value = answers.get("answer")
        return options.index(value) if value in options else default

    def confirm(self, prompt: str) -> bool:
        from xli.ui.questionnaire.base import Question

        answers = self.ask([Question(id="answer", question=prompt, type="confirm")])
        # Only an explicit yes is a yes. Anything else -- including an answer the
        # front end could not collect -- is a no.
        return answers.get("answer") == "yes"
