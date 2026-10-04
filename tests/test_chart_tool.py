#!/usr/bin/env python3
"""The `chart` tool and the chart vocabulary behind it.

The agent collects numbers constantly and, before this, could only describe
them in prose. These tests pin the shapes: every kind renders text, the text
survives a narrow terminal, numbers read like Russian (`2,3 млн`, not `2.3M`),
and bad input is an error the model can read rather than a traceback.
"""

import asyncio
import json

import pytest

from xli.tools.charts_tool import KINDS, chart, render
from xli.ui import charts
from xli.ui.locale import number_word, set_lang


@pytest.fixture(autouse=True)
def russian():
    set_lang("ru")
    yield
    set_lang(None)


class TestEveryKindRenders:
    def test_bar(self):
        lines = render("bar", items=[["read", 12], ["bash", 3]])
        assert len(lines) == 2
        assert "read" in lines[0], "biggest first"
        assert "█" in lines[0]

    def test_line(self):
        lines = render("line", series=[1, 5, 2, 8, 3])
        assert lines and all(char in "" or char for char in lines[0])
        assert any(char in "⣿⣷⣯⡇⢸⠉⠒" for line in lines for char in line)

    def test_spark(self):
        assert render("spark", series=[1, 2, 3])[0]

    def test_hist(self):
        lines = render("hist", series=[1, 1, 2, 2, 2, 5, 9])
        assert len(lines) >= 2

    def test_timeline(self):
        lines = render("timeline", items=[["read", 2.0], ["bash", 8.0]])
        assert len(lines) == 2
        assert len(lines[1]) > len(lines[0]), "the longer step draws a longer bar"

    def test_stacked(self):
        lines = render("stacked", items=[["модуль", [3, 1, 0.5]], ["тест", [1, 1, 1]]])
        assert len(lines) == 2

    def test_tree(self):
        lines = render("tree", items=[["агент", 0], ["шаг 1", 1], ["read", 2]])
        assert lines[0].startswith("◆")
        assert lines[2].startswith("│")

    def test_heat(self):
        lines = render("heat", matrix=[[1, 2, 3], [3, 3, 3]], row_labels=["a", "b"])
        assert len(lines) == 2
        assert lines[0].startswith("a")

    def test_treemap(self):
        lines = render("treemap", items=[["xli", 240], ["tests", 120]], width=40, height=6)
        assert len(lines) >= 7
        assert any("xli" in line for line in lines), "the biggest block is labelled"

    def test_table(self):
        lines = render("table", rows=[["read", 12], ["bash", 3]], headers=["инстр.", "раз"])
        assert lines[0].split() == ["инстр.", "раз"]
        assert lines[1].startswith("─")
        assert lines[2].startswith("read")

    def test_gauge(self):
        lines = render("gauge", value=0.5, title="контекст")
        assert "50%" in lines[0]
        assert "контекст" in lines[0]

    def test_title_is_printed_once(self):
        lines = render("bar", items=[["a", 1]], title="подпись")
        assert lines[0] == "подпись"
        assert lines.count("подпись") == 1


class TestRussianNumbers:
    def test_millions_use_a_comma_and_the_short_form(self):
        assert number_word(2_300_000) == "2,3 млн"
        assert number_word(45_231) == "45 тыс"

    def test_small_numbers_stay_plain(self):
        assert number_word(12) == "12"

    def test_english_still_says_m(self):
        set_lang("en")
        assert number_word(2_300_000) == "2.3M"
        set_lang("ru")

    def test_a_bar_shows_the_russian_form(self):
        lines = charts.bars([("модули", 2_300_000)])
        assert "2,3 млн" in lines[0]


class TestBadInputIsAnError:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"kind": "нет_такого", "items": [["a", 1]]},
            {"kind": "bar", "items": []},
            {"kind": "bar", "items": [["a", "не число"]]},
            {"kind": "heat"},
            {"kind": "table"},
            {"kind": "gauge"},
        ],
    )
    def test_errors_are_tool_errors(self, kwargs):
        from xli.tools.base import ToolError

        with pytest.raises(ToolError):
            render(**kwargs)

    def test_json_is_parsed_from_the_tool_arguments(self):
        result = asyncio.run(chart.run(kind="bar", items='[["a", 2], ["b", 1]]'))
        assert result.ok
        assert "a" in result.summary

    def test_broken_json_says_so(self):
        """The tool layer turns a ToolError into a result the model can read."""
        result = asyncio.run(chart.run(kind="bar", items="[{oops"))
        assert result.ok is False
        assert "not valid JSON" in (result.error or "")

    def test_a_chart_is_not_mutating(self):
        result = asyncio.run(chart.run(kind="spark", series="[1, 2, 3]"))
        assert result.ok
        from xli.tools.charts_tool import CHART_TOOL

        assert CHART_TOOL.spec.mutates is False


class TestRegistered:
    def test_chart_is_in_the_default_registry(self):
        from xli.tools.registry import default_registry

        assert "chart" in default_registry().names()

    def test_the_model_is_told_every_kind(self):
        from xli.tools.charts_tool import CHART_TOOL

        description = CHART_TOOL.spec.description
        for kind in KINDS:
            assert kind in description, f"{kind} must be discoverable from the prompt"

    def test_the_build_toolset_can_chart(self):
        from xli.core.plan_build import BUILD_TOOLS

        assert "chart" in BUILD_TOOLS


class TestWidths:
    @pytest.mark.parametrize("width", [20, 40, 60, 96])
    def test_the_bar_is_exactly_as_wide_as_asked(self, width):
        """Labels sit to the left of the bar, so the bar must not inherit the
        label's width — that is how a chart overflows a narrow terminal."""
        line = render("bar", items=[["очень длинное имя", 5]], width=width)[0]
        bar = line.split("  ", 1)[1].rsplit(" ", 1)[0]
        assert len(bar) == width

    def test_a_narrow_treemap_gives_up_instead_of_breaking(self):
        assert render("treemap", items=[["a", 1]], width=6, height=2) == []


class TestOutputIsCopyable:
    def test_no_ansi_escapes_in_chart_text(self):
        lines = render("bar", items=[["a", 1], ["b", 2]])
        assert "\x1b" not in "\n".join(lines)

    def test_chart_text_is_plain_utf8(self):
        lines = render("tree", items=[["верх", 0], ["низ", 1]])
        assert all(isinstance(line, str) for line in lines)
        assert json.dumps(lines, ensure_ascii=False)
