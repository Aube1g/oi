#!/usr/bin/env python3
"""Clarifying questions before a task runs.

    from xli.ui.questionnaire import Question, TerminalQuestionnaire

    answers = TerminalQuestionnaire().run([
        Question(id="scope", question="Which files?", type="choice",
                 options=["this file", "the package", "everything"]),
        Question(id="tests", question="Run tests after?", type="confirm", default="y"),
    ])

A vague task is the main reason an agent does the wrong thing confidently, so
asking two questions up front is usually cheaper than undoing a bad run. The
model in `base.py` owns what a question means and what counts as an answer; the
front ends here own only the asking, so the same definition behaves the same in
a terminal, the TUI and Neovim.
"""

from __future__ import annotations

from xli.ui.questionnaire.base import CHOICE, CONFIRM, TEXT, TYPES, Question
from xli.ui.questionnaire.nvim import NvimQuestionnaire
from xli.ui.questionnaire.terminal import TerminalQuestionnaire

__all__ = [
    "CHOICE",
    "CONFIRM",
    "NvimQuestionnaire",
    "Question",
    "TEXT",
    "TYPES",
    "TerminalQuestionnaire",
]
