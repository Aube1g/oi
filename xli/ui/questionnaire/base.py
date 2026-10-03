#!/usr/bin/env python3
"""The question model shared by every questionnaire front end.

Three front ends ask these questions -- terminal, TUI and Neovim -- and they
must agree on what a question means, or the same definition behaves differently
depending on where it is run. So the model owns the semantics (what counts as a
valid answer, what a default is) and the front ends own only the asking.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The kinds of question a front end must be able to ask.
TEXT = "text"
CHOICE = "choice"
CONFIRM = "confirm"
TYPES = (TEXT, CHOICE, CONFIRM)

#: What a confirm question accepts, and what each means.
YES = ("y", "yes", "да", "д")
NO = ("n", "no", "нет", "н")


@dataclass
class Question:
    """One question.

    `type` decides how the answer is read and validated. It used to be declared
    and then ignored -- every front end branched on `options` being non-empty
    instead -- so a question marked `confirm` was asked as free text and
    recorded whatever the user happened to type.
    """

    id: str
    question: str
    type: str = TEXT
    options: list[str] = field(default_factory=list)
    default: str | None = None
    required: bool = True
    #: Free text shown under the question. For the "why am I being asked this"
    #: that stops a questionnaire feeling like a form with no purpose.
    hint: str = ""

    def __post_init__(self) -> None:
        if self.type not in TYPES:
            raise ValueError(f"question {self.id!r}: type must be one of {', '.join(TYPES)}")
        if self.type == CHOICE and not self.options:
            raise ValueError(f"question {self.id!r}: a choice question needs options")
        if self.type == CONFIRM and self.default not in (None, *YES, *NO):
            raise ValueError(
                f"question {self.id!r}: a confirm default must be a yes/no word, "
                f"got {self.default!r}"
            )

    # ------------------------------------------------------------------ answers
    def normalise(self, raw: str | None) -> str | None:
        """Coerce a raw answer into the value that gets recorded.

        Returns None when the answer is empty and there is no default, which is
        how a caller tells "skipped" apart from "answered with an empty string".
        """
        text = (raw or "").strip()
        # Computed once, up front: the choice branch compares against it too,
        # and defining it inside the confirm branch meant answering a choice
        # question with its option text raised UnboundLocalError.
        lowered = text.lower()

        if self.type == CONFIRM:
            if not text:
                if self.default is None:
                    return None
                return "yes" if self.default.lower() in YES else "no"
            if lowered in YES:
                return "yes"
            if lowered in NO:
                return "no"
            # Anything else is not a yes and not a no; saying "yes" would be a
            # guess about a permission the user did not clearly grant.
            return None

        if self.type == CHOICE:
            if not text:
                return self.default
            # Accept either the number shown or the option text itself.
            if text.isdigit():
                index = int(text) - 1
                if 0 <= index < len(self.options):
                    return self.options[index]
                return None
            for option in self.options:
                if option.lower() == lowered:
                    return option
            return None

        if not text:
            return self.default
        return text

    def is_acceptable(self, value: str | None) -> bool:
        """Whether a normalised value may be recorded."""
        if value is None or value == "":
            return not self.required
        return True

    def prompt(self) -> str:
        """The question line, with its default shown the way users expect."""
        if self.type == CONFIRM:
            default = (self.default or "").lower()
            marks = "[Y/n]" if default in YES else "[y/N]" if default in NO else "[y/n]"
            return f"{self.question} {marks}"
        if self.type == CHOICE:
            return self.question
        if self.default:
            return f"{self.question} [{self.default}]"
        return self.question

    def numbered_options(self) -> list[str]:
        """Options as they should be displayed, one per line."""
        return [f"{index}. {option}" for index, option in enumerate(self.options, start=1)]
