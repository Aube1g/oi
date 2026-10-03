#!/usr/bin/env python3
"""The Neovim questionnaire.

Neovim already has a UI, so this front end does not draw one: it hands each
question to `vim.ui.select` or `vim.ui.input` through the injected bridge and
reads the answer back. That keeps the appearance consistent with the rest of the
editor, which is the reason to run xli inside Neovim at all.

When there is no bridge -- xli invoked from a shell, or a Neovim without the
plugin -- it falls back to the terminal questionnaire rather than failing, since
the answers matter more than where they were collected.
"""

from __future__ import annotations

from typing import Any

from xli.core.logger import get_logger
from xli.ui.questionnaire.base import Question
from xli.ui.questionnaire.terminal import TerminalQuestionnaire

logger = get_logger("ui.questionnaire.nvim")

COMPONENT = "questionnaire.nvim"


class NvimQuestionnaire:
    """Asks questions through a Neovim UI bridge.

    `nvim_ui` must provide `is_available()`, `ask_choice(prompt, options,
    default_index)` and `ask_input(prompt, default)`. It is injected rather than
    constructed here so this class stays importable -- and testable -- with no
    Neovim present.
    """

    def __init__(self, nvim_ui: Any = None):
        self.nvim_ui = nvim_ui

    # ------------------------------------------------------------------ helpers
    def _available(self) -> bool:
        if self.nvim_ui is None:
            return False
        probe = getattr(self.nvim_ui, "is_available", None)
        if probe is None:
            return False
        try:
            return bool(probe())
        except Exception:  # pragma: no cover - a broken bridge must not raise
            logger.log_structured(
                "WARNING", COMPONENT, "nvim bridge raised on is_available"
            )
            return False

    # ---------------------------------------------------------------------- run
    def run(self, questions: list[Question]) -> dict[str, str]:
        """Ask every question, returning answers keyed by question id.

        Synchronous, like the terminal front end. It used to delegate to an
        `async def` without awaiting it, so the fallback returned a coroutine
        object instead of a dict of answers.
        """
        if not questions:
            return {}

        if not self._available():
            logger.log_structured(
                "WARNING", COMPONENT, "Neovim unavailable, falling back to terminal"
            )
            return TerminalQuestionnaire().run(questions)

        answers: dict[str, str] = {}
        for question in questions:
            answers[question.id] = self._ask(question)

        logger.log_structured(
            "INFO",
            COMPONENT,
            "questionnaire complete",
            {"questions": len(questions), "answers": len(answers)},
        )
        return answers

    def _ask(self, question: Question) -> str:
        """Ask one question through the bridge, normalising whatever comes back."""
        if question.type == "choice" and question.options:
            default_index = 0
            if question.default in question.options:
                default_index = question.options.index(question.default)
            try:
                index = self.nvim_ui.ask_choice(
                    question.prompt(), question.options, default_index
                )
            except Exception as exc:  # pragma: no cover - bridge failure
                logger.log_structured(
                    "WARNING", COMPONENT, f"ask_choice failed for {question.id}: {exc}"
                )
                return question.default or ""
            if isinstance(index, int) and 0 <= index < len(question.options):
                return question.options[index]
            return question.default or ""

        try:
            raw = self.nvim_ui.ask_input(question.prompt(), question.default or "")
        except Exception as exc:  # pragma: no cover - bridge failure
            logger.log_structured(
                "WARNING", COMPONENT, f"ask_input failed for {question.id}: {exc}"
            )
            return question.default or ""

        # The bridge may return None on cancel; normalise() treats that as "no
        # answer", which is_acceptable then judges against `required`.
        value = question.normalise(raw if isinstance(raw, str) else None)
        if question.is_acceptable(value):
            return value or ""
        return question.default or ""
