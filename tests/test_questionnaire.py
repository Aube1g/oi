"""Tests for the questionnaire and the UI port.

The property that matters most here is that nothing fabricates an answer. A
question the user was never shown must not come back as "yes", so the tests
assert on refusals as much as on successes.
"""

import io

import pytest

from xli.ui.port import TerminalUi, UiPort
from xli.ui.questionnaire import (
    CHOICE,
    CONFIRM,
    TEXT,
    NvimQuestionnaire,
    Question,
    TerminalQuestionnaire,
)


def scripted(lines):
    """A reader that stands in for a person typing, then runs out."""
    iterator = iter(lines)
    return lambda prompt: next(iterator)


def run(questions, lines):
    """Run the terminal questionnaire against scripted input."""
    out = io.StringIO()
    questionnaire = TerminalQuestionnaire(reader=scripted(lines), out=out)
    return questionnaire.run(questions), out.getvalue()


# ---------------------------------------------------------------- the question
class TestQuestionValidation:
    def test_a_plain_text_question_is_accepted(self):
        assert Question(id="a", question="q").type == TEXT

    def test_unknown_type_is_rejected(self):
        with pytest.raises(ValueError, match="type must be one of"):
            Question(id="a", question="q", type="slider")

    def test_choice_without_options_is_rejected(self):
        with pytest.raises(ValueError, match="needs options"):
            Question(id="a", question="q", type=CHOICE)

    def test_choice_with_options_is_accepted(self):
        question = Question(id="a", question="q", type=CHOICE, options=["x", "y"])
        assert question.options == ["x", "y"]

    def test_confirm_default_must_be_yes_or_no(self):
        with pytest.raises(ValueError, match="yes/no"):
            Question(id="a", question="q", type=CONFIRM, default="maybe")

    @pytest.mark.parametrize("default", ["y", "yes", "n", "no", "да", "нет", None])
    def test_confirm_accepts_a_yes_no_default(self, default):
        assert Question(id="a", question="q", type=CONFIRM, default=default).default == default


class TestNormalise:
    def test_text_returns_what_was_typed(self):
        assert Question(id="a", question="q").normalise("  hello  ") == "hello"

    def test_text_falls_back_to_the_default(self):
        assert Question(id="a", question="q", default="d").normalise("") == "d"

    def test_text_with_no_default_and_no_answer_is_none(self):
        # None means "no answer", which is how a caller tells skipped from empty.
        assert Question(id="a", question="q").normalise("") is None

    @pytest.mark.parametrize(
        "raw,want",
        [("1", "this file"), ("2", "the package"), ("the package", "the package"),
         ("THIS FILE", "this file"), ("9", None), ("nope", None)],
    )
    def test_choice_accepts_a_number_or_the_option_text(self, raw, want):
        question = Question(
            id="s", question="scope?", type=CHOICE, options=["this file", "the package"]
        )
        assert question.normalise(raw) == want

    @pytest.mark.parametrize(
        "raw,want",
        [("y", "yes"), ("Y", "yes"), ("yes", "yes"), ("да", "yes"), ("д", "yes"),
         ("n", "no"), ("no", "no"), ("нет", "no"), ("н", "no"), ("maybe", None)],
    )
    def test_confirm_maps_to_yes_or_no(self, raw, want):
        assert Question(id="t", question="q", type=CONFIRM).normalise(raw) == want

    def test_confirm_uses_its_default_when_blank(self):
        assert Question(id="t", question="q", type=CONFIRM, default="y").normalise("") == "yes"
        assert Question(id="t", question="q", type=CONFIRM, default="n").normalise("") == "no"

    def test_confirm_with_no_default_and_no_answer_is_none(self):
        assert Question(id="t", question="q", type=CONFIRM).normalise("") is None


class TestAcceptability:
    def test_required_question_rejects_an_empty_answer(self):
        assert Question(id="a", question="q").is_acceptable("") is False

    def test_optional_question_accepts_an_empty_answer(self):
        assert Question(id="a", question="q", required=False).is_acceptable("") is True

    def test_a_real_answer_is_always_acceptable(self):
        assert Question(id="a", question="q").is_acceptable("x") is True


class TestPromptRendering:
    def test_confirm_shows_the_default_side(self):
        assert "[Y/n]" in Question(id="a", question="q", type=CONFIRM, default="y").prompt()
        assert "[y/N]" in Question(id="a", question="q", type=CONFIRM, default="n").prompt()

    def test_text_shows_its_default(self):
        assert "[main]" in Question(id="a", question="branch?", default="main").prompt()

    def test_options_are_numbered_from_one(self):
        question = Question(id="a", question="q", type=CHOICE, options=["x", "y"])
        assert question.numbered_options() == ["1. x", "2. y"]


# ------------------------------------------------------------- terminal front end
class TestTerminalQuestionnaire:
    def test_every_question_appears_in_the_result(self):
        # A missing key is ambiguous: not asked, or asked and skipped?
        questions = [
            Question(id="a", question="one?", required=False),
            Question(id="b", question="two?", required=False),
        ]
        answers, _ = run(questions, ["", ""])
        assert set(answers) == {"a", "b"}

    def test_choice_records_the_option_text(self):
        answers, _ = run(
            [Question(id="s", question="scope?", type=CHOICE, options=["one", "two"])], ["2"]
        )
        assert answers["s"] == "two"

    def test_confirm_records_yes_or_no(self):
        answers, _ = run([Question(id="t", question="ok?", type=CONFIRM)], ["y"])
        assert answers["t"] == "yes"

    def test_required_question_is_re_asked(self):
        answers, transcript = run([Question(id="a", question="must?")], ["", "", "real"])
        assert answers["a"] == "real"
        assert transcript.count("an answer is required") == 2

    def test_out_of_range_choice_is_re_asked(self):
        answers, transcript = run(
            [Question(id="s", question="q", type=CHOICE, options=["a", "b"])], ["9", "1"]
        )
        assert answers["s"] == "a"
        assert "pick one of 1-2" in transcript

    def test_unparseable_confirm_is_re_asked(self):
        answers, transcript = run(
            [Question(id="t", question="ok?", type=CONFIRM)], ["maybe", "n"]
        )
        assert answers["t"] == "no"
        assert "answer y or n" in transcript

    def test_optional_question_may_be_skipped(self):
        answers, _ = run([Question(id="a", question="q", required=False)], [""])
        assert answers["a"] == ""

    def test_eof_yields_the_default_instead_of_raising(self):
        def eof(prompt):
            raise EOFError

        out = io.StringIO()
        answers = TerminalQuestionnaire(reader=eof, out=out).run(
            [Question(id="x", question="q", default="fallback")]
        )
        assert answers == {"x": "fallback"}

    def test_keyboard_interrupt_is_treated_like_eof(self):
        def interrupt(prompt):
            raise KeyboardInterrupt

        answers = TerminalQuestionnaire(reader=interrupt, out=io.StringIO()).run(
            [Question(id="x", question="q", default="d")]
        )
        assert answers == {"x": "d"}

    def test_no_questions_is_a_no_op(self):
        out = io.StringIO()
        assert TerminalQuestionnaire(reader=scripted([]), out=out).run([]) == {}
        assert out.getvalue() == ""

    def test_hint_is_shown(self):
        _, transcript = run(
            [Question(id="a", question="q", hint="because it matters", required=False)], ["x"]
        )
        assert "because it matters" in transcript

    def test_banner_is_plain_ascii(self):
        # The old banner's emoji were the first thing to turn into mojibake on a
        # console that was not UTF-8.
        _, transcript = run([Question(id="a", question="q", required=False)], [""])
        assert transcript.isascii()


# ------------------------------------------------------------------ nvim front end
class TestNvimQuestionnaire:
    def test_no_bridge_falls_back_to_a_dict(self):
        # It used to call an `async def` without awaiting, returning a coroutine.
        result = NvimQuestionnaire(nvim_ui=None).run(
            [Question(id="x", question="q", default="d")]
        )
        assert isinstance(result, dict)

    def test_no_questions_is_a_no_op(self):
        assert NvimQuestionnaire(nvim_ui=None).run([]) == {}

    def test_bridge_choice_is_used(self):
        class Bridge:
            def is_available(self):
                return True

            def ask_choice(self, prompt, options, default):
                return 1

            def ask_input(self, prompt, default):
                return default

        answers = NvimQuestionnaire(Bridge()).run(
            [Question(id="s", question="q", type=CHOICE, options=["a", "b", "c"])]
        )
        assert answers["s"] == "b"

    def test_bridge_input_is_normalised(self):
        class Bridge:
            def is_available(self):
                return True

            def ask_choice(self, prompt, options, default):
                return default

            def ask_input(self, prompt, default):
                return "yes"

        answers = NvimQuestionnaire(Bridge()).run(
            [Question(id="t", question="q", type=CONFIRM)]
        )
        assert answers["t"] == "yes"

    def test_a_cancelled_bridge_choice_uses_the_default(self):
        class Bridge:
            def is_available(self):
                return True

            def ask_choice(self, prompt, options, default):
                return None

            def ask_input(self, prompt, default):
                return default

        answers = NvimQuestionnaire(Bridge()).run(
            [Question(id="s", question="q", type=CHOICE, options=["a", "b"], default="a")]
        )
        assert answers["s"] == "a"

    def test_a_raising_bridge_does_not_propagate(self):
        class Broken:
            def is_available(self):
                return True

            def ask_choice(self, prompt, options, default):
                raise RuntimeError("bridge died")

            def ask_input(self, prompt, default):
                raise RuntimeError("bridge died")

        answers = NvimQuestionnaire(Broken()).run(
            [Question(id="x", question="q", default="d")]
        )
        assert answers == {"x": "d"}

    def test_is_available_raising_is_treated_as_unavailable(self):
        class Flaky:
            def is_available(self):
                raise RuntimeError("no")

        # Falls back rather than raising.
        assert isinstance(NvimQuestionnaire(Flaky()).run([]), dict)


# ---------------------------------------------------------------------- the port
class TestUiPortRefusesToGuess:
    """The regression this module exists to prevent."""

    def test_cannot_ask_by_default(self):
        assert UiPort.can_ask is False

    @pytest.mark.parametrize(
        "method,args",
        [
            ("ask", ([Question(id="a", question="q")],)),
            ("input", ("q",)),
            ("choice", ("q", ["a", "b"])),
            ("confirm", ("q",)),
        ],
    )
    def test_asking_raises_rather_than_answering(self, method, args):
        with pytest.raises(NotImplementedError):
            getattr(UiPort(), method)(*args)

    def test_confirm_does_not_default_to_true(self):
        # The adapter this replaced returned True unconditionally, which wired
        # into a permission prompt is auto-approval of every tool call.
        with pytest.raises(NotImplementedError):
            UiPort().confirm("delete everything?")

    @pytest.mark.parametrize("method,args", [
        ("display", ("hi",)), ("notify", ("hi",)), ("progress", (50,)), ("clear", ()),
    ])
    def test_output_methods_are_safe_no_ops(self, method, args):
        assert getattr(UiPort(), method)(*args) is None


class TestTerminalUi:
    def _ui(self, lines):
        ui = TerminalUi.__new__(TerminalUi)
        ui._out = io.StringIO()
        iterator = iter(lines)
        ui.ask = lambda questions: TerminalQuestionnaire(
            reader=lambda prompt: next(iterator), out=ui._out
        ).run(list(questions))
        return ui

    def test_can_ask(self):
        assert TerminalUi.can_ask is True

    @pytest.mark.parametrize(
        "answer,want", [("y", True), ("yes", True), ("да", True), ("n", False),
                        ("no", False), ("нет", False)]
    )
    def test_confirm_reads_the_answer(self, answer, want):
        assert self._ui([answer]).confirm("ok?") is want

    def test_confirm_after_eof_is_false(self):
        ui = TerminalUi.__new__(TerminalUi)
        ui._out = io.StringIO()

        def eof(prompt):
            raise EOFError

        ui.ask = lambda questions: TerminalQuestionnaire(reader=eof, out=ui._out).run(
            list(questions)
        )
        # No explicit yes means no.
        assert ui.confirm("ok?") is False

    def test_choice_by_number_and_by_text(self):
        assert self._ui(["2"]).choice("pick", ["a", "b", "c"]) == 1
        assert self._ui(["b"]).choice("pick", ["a", "b", "c"]) == 1

    def test_choice_falls_back_to_the_default_index(self):
        assert self._ui([""]).choice("pick", ["a", "b", "c"], default=2) == 2

    def test_choice_with_no_options_is_refused(self):
        with pytest.raises(ValueError, match="at least one option"):
            self._ui(["x"]).choice("pick", [])

    def test_input_returns_the_text(self):
        assert self._ui(["hello"]).input("name?") == "hello"

    def test_input_falls_back_to_the_default(self):
        assert self._ui([""]).input("name?", default="anon") == "anon"

    def test_display_with_a_title(self):
        ui = TerminalUi(out=io.StringIO())
        ui.display("body", title="Head")
        text = ui._out.getvalue()
        assert "Head" in text and "body" in text

    def test_notify_and_progress_do_not_raise(self):
        ui = TerminalUi(out=io.StringIO())
        ui.notify("hello", level="warn")
        ui.progress(40, "working")
        assert "hello" in ui._out.getvalue()
