#!/usr/bin/env python3
"""The kernel build's animated view: Russian, aligned, and honest.

Three things went wrong here before:

  * the view was English (`XLI Kernel Build`, `built:`, `FAIL:`) in an
    interface that is Russian everywhere else;
  * rows were padded with `f"{text:<{width}}"` on strings containing colour
    escapes, which counts escape bytes as visible cells — the right border
    landed in a different column on every row, and the box looked broken;
  * a module that failed printed as a bare `✗ name` with no timing and no
    place in the summary's slowest list.

The tests render into a buffer and measure visible width, which is the only
measurement that matters for a box.
"""

import io
import re

import pytest

from xli.manager.kernel_build import BuildReport
from xli.tui.kernel_anim import KernelBuildAnimator
from xli.ui.locale import set_lang
from xli.ui.text import display_width

ANSI = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


def plain(text: str) -> str:
    return ANSI.sub("", text)


def visible_lines(text: str) -> list[str]:
    return [line for line in plain(text).split("\n") if line.strip()]


def stream() -> io.StringIO:
    buffer = io.StringIO()
    buffer.isatty = lambda: True  # type: ignore[method-assign]
    return buffer


def driven(events, report=None) -> tuple[str, str]:
    """Run an animator over events; return (frames, summary)."""
    buffer = stream()
    animator = KernelBuildAnimator(stream=buffer)
    for event in events:
        animator.on_progress(event)
    frames = buffer.getvalue()
    summary = ""
    if report is not None:
        out = stream()
        KernelBuildAnimator(stream=out).finish(report)
        summary = out.getvalue()
    return frames, summary


@pytest.fixture(autouse=True)
def russian():
    set_lang("ru")
    yield
    set_lang(None)


EVENTS = [
    {"phase": "preflight"},
    {"phase": "cythonize", "module_names": ["parser", "agent", "cli"]},
    {"phase": "compile"},
    {"module_start": "parser"},
    {"module_done": "parser"},
    {"module_start": "agent"},
    {"module_fail": "agent", "error": "Cython SyntaxError"},
    {"module_start": "cli"},
    {"module_done": "cli"},
    {"phase": "link"},
]

OK_REPORT = BuildReport(
    ok=True,
    built=["parser", "cli"],
    failed=[{"module": "agent", "error": "Cython SyntaxError"}],
    seconds=8.4,
    durations={"parser": 2.1, "cli": 3.2},
    artifacts={"parser": 421888, "cli": 655360},
    log="warning: a compiler warning",
)


class TestRussianOnly:
    def test_the_frame_has_no_english(self):
        frames, _ = driven(EVENTS)
        body = plain(frames)
        for word in ("Kernel Build", "preflight", "cythonize", "compile", "link", "Building"):
            assert word not in body, f"{word!r} is not Russian"

    def test_the_summary_has_no_english(self):
        _, summary = driven([], OK_REPORT)
        body = plain(summary)
        for word in ("built", "skipped", "FAIL", "kernel compiled", "error(s)"):
            assert word not in body

    def test_phases_read_as_work(self):
        frames, _ = driven(EVENTS)
        body = plain(frames)
        assert "проверка" in body
        assert "компиляция" in body or "сборка" in body

    def test_the_summary_counts_modules_in_russian(self):
        _, summary = driven([], OK_REPORT)
        assert "собрано 2 модуля" in plain(summary)

    def test_file_counts_agree_with_their_number(self):
        for count, word in ((1, "файл"), (2, "файла"), (5, "файлов")):
            report = BuildReport(
                ok=True, built=["m"] * count, seconds=1.0,
                artifacts={f"m{index}": 1024 for index in range(count)},
            )
            _, summary = driven([], report)
            assert f"{count} {word}" in plain(summary)

    def test_sizes_are_in_binary_units_with_a_comma(self):
        _, summary = driven([], OK_REPORT)
        body = plain(summary)
        assert "КиБ" in body or "МиБ" in body
        assert "8,4 с" in body, "Russian decimal comma"


class TestTheBoxIsAligned:
    def test_every_frame_row_is_the_same_width(self):
        frames, _ = driven(EVENTS)
        widths = {display_width(line) for line in visible_lines(frames)}
        assert len(widths) == 1, f"ragged frame: {sorted(widths)}"

    def test_every_summary_row_is_the_same_width(self):
        _, summary = driven([], OK_REPORT)
        widths = {display_width(line) for line in visible_lines(summary)}
        assert len(widths) == 1, f"ragged summary: {sorted(widths)}"

    def test_a_long_module_name_does_not_break_the_box(self):
        events = [
            {"phase": "compile", "module_names": ["a-really-extremely-long-module-name-here"]},
            {"module_start": "a-really-extremely-long-module-name-here"},
        ]
        frames, _ = driven(events)
        widths = {display_width(line) for line in visible_lines(frames)}
        assert len(widths) == 1

    def test_the_summary_survives_a_narrow_terminal(self, monkeypatch):
        monkeypatch.setattr("shutil.get_terminal_size", lambda *a, **k: (42, 24))
        buffer = stream()
        KernelBuildAnimator(stream=buffer).finish(OK_REPORT)
        widths = {display_width(line) for line in visible_lines(buffer.getvalue())}
        assert len(widths) == 1, f"narrow terminal tears the box: {sorted(widths)}"


class TestWhatTheViewSays:
    def test_a_module_that_finished_is_checked_and_timed(self):
        frames, _ = driven(EVENTS)
        body = plain(frames)
        assert "✓ parser" in body

    def test_a_module_that_failed_is_marked_and_named_in_the_summary(self):
        _, summary = driven([], OK_REPORT)
        body = plain(summary)
        assert "✗ agent" in body
        assert "Cython SyntaxError" in body

    def test_the_summary_lists_where_the_time_went(self):
        _, summary = driven([], OK_REPORT)
        body = plain(summary)
        assert "дольше всех" in body
        assert "cli" in body and "3,2" in body

    def test_warnings_are_counted(self):
        _, summary = driven([], OK_REPORT)
        assert "предупреждений" in plain(summary)

    def test_progress_shows_the_count_not_only_a_percentage(self):
        frames, _ = driven(EVENTS)
        assert "из" in plain(frames), "3 из 4 says more than 75%"

    def test_an_empty_build_says_so(self):
        _, summary = driven([], BuildReport(ok=True, seconds=0.5))
        assert "нечего собирать" in plain(summary)


class TestNonInteractive:
    def test_a_plain_stream_gets_lines_not_escapes(self):
        """A build log redirected to a file must not contain cursor games."""
        buffer = io.StringIO()
        animator = KernelBuildAnimator(stream=buffer)
        for event in EVENTS:
            animator.on_progress(event)
        out = buffer.getvalue()
        assert "\033[?25l" not in out
        assert "parser" in out

    def test_finish_on_a_plain_stream_prints_the_summary(self):
        buffer = io.StringIO()
        KernelBuildAnimator(stream=buffer).finish(OK_REPORT)
        assert "собрано" in buffer.getvalue()


class TestReportFields:
    def test_slowest_orders_by_time(self):
        assert OK_REPORT.slowest(2)[0][0] == "cli"

    def test_bytes_built_sums_the_artifacts(self):
        assert OK_REPORT.bytes_built == 421888 + 655360

    def test_to_dict_carries_the_new_fields(self):
        data = OK_REPORT.to_dict()
        assert data["durations"]["parser"] == 2.1
        assert data["artifacts"]["cli"] == 655360
