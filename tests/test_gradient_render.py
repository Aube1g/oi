#!/usr/bin/env python3
"""Tests for the installer UI renderers and the ANSI painter.

Same story as ``test_gradient_demo.py``: this file used to print its output by
failing on purpose. The assertions below are what "the render works" should
have meant all along — colour where colour is legal, plain text where it is
not, and frames that are exactly the width they claim.
"""

import re

from xli.ui.ansi import ANSI_FOR_STYLE, paint_span, render_markdown_ansi
from xli.ui.installer import (
    SPINNER_FRAMES,
    render_dep_card,
    render_gradient_line,
    render_progress_bar,
    render_stage_header,
    render_summary,
)

ANSI = re.compile(r"\033\[[0-9;]*m")


class FakeDep:
    name = "httpx"
    description = "Modern async HTTP client for Python"
    why_needed = "Web search, API calls to SearXNG"
    required_by = ["web_search", "fetch_page"]
    install_spec = "httpx>=0.27"


class TestGradientLine:
    def test_plain_when_disabled(self, monkeypatch):
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        assert "\033[" not in render_gradient_line(30)

    def test_text_is_centred_and_kept(self):
        line = render_gradient_line(30, "INSTALLING")
        assert "INSTALLING" in line
        assert ANSI.sub("", line).strip().startswith("─")

    def test_narrow_width_does_not_crash(self):
        assert "AB" in render_gradient_line(4, "AB")

    def test_wide_width_is_exact(self, monkeypatch):
        monkeypatch.setenv("FORCE_COLOR", "1")
        line = render_gradient_line(40)
        assert len(ANSI.sub("", line)) == 40


class TestProgressBar:
    def test_plain_when_not_a_tty(self):
        bar = ANSI.sub("", render_progress_bar(40))
        assert bar.startswith("[") and bar.endswith("]  40%")
        assert bar.count("█") + bar.count("▓") + bar.count("░") == 30

    def test_zero_and_hundred_do_not_misplace_the_head(self):
        empty = ANSI.sub("", render_progress_bar(0))
        full = ANSI.sub("", render_progress_bar(100))
        assert "▓" not in empty
        assert "▓" not in full
        assert "░" not in full

    def test_colour_when_forced(self, monkeypatch):
        monkeypatch.setenv("FORCE_COLOR", "1")
        assert "\033[" in render_progress_bar(50)


class TestStageHeader:
    def test_known_stage(self):
        assert "INSTALL" in render_stage_header("install")

    def test_unknown_stage_still_renders(self):
        assert "SOMETHING" in render_stage_header("something")

    def test_no_emoji_in_the_palette(self):
        """Emoji are one or two cells depending on the terminal.

        A progress line that changes width between terminals overwrites itself,
        so the palette stays inside the dingbat/box-drawing ranges.
        """
        line = render_stage_header("install")
        assert all(ord(ch) < 0x1F000 for ch in line if not ch.isspace())


class TestDependencyCard:
    def test_card_lists_the_fields(self):
        card = render_dep_card(FakeDep())
        for field in ("httpx", "Modern async HTTP client", "web_search", "pip install httpx"):
            assert field in card

    def test_card_lines_are_uniformly_wide(self):
        widths = {len(ANSI.sub("", line)) for line in render_dep_card(FakeDep()).split("\n")}
        assert len(widths) == 1, f"ragged card: {sorted(widths)}"


class TestSummary:
    def test_counts_ok_and_failed(self):
        out = render_summary({"httpx": True, "rich": True, "textual": False})
        assert "Total: 2 installed, 1 failed/skipped" in out

    def test_marks_are_not_emoji(self):
        out = render_summary({"httpx": True})
        assert "✓" in out and "✅" not in out


class TestSpinnerFrames:
    def test_frames_are_one_cell_each(self):
        from xli.ui.text import display_width

        assert all(display_width(frame) == 1 for frame in SPINNER_FRAMES)

    def test_at_least_ten_frames(self):
        assert len(SPINNER_FRAMES) >= 10


class TestAnsiPainter:
    def test_headings_glow_per_character(self):
        out = render_markdown_ansi("# Заголовок", 40, enabled=True)
        # A flat heading carries two escapes (one on, one off). A gradient
        # emits several colour changes as the ramp walks the text.
        assert out.count("\033[") > 4

    def test_disabled_returns_plain_text(self):
        out = render_markdown_ansi("# Заголовок\n\nтекст", 40, enabled=False)
        assert "\033[" not in out
        assert "Заголовок" in out and "текст" in out

    def test_rule_is_a_single_gradient_run(self):
        out = render_markdown_ansi("---", 30, enabled=True)
        assert "─" * 30 == ANSI.sub("", out).strip()

    def test_every_markdown_style_has_a_code(self):
        expected = {
            "normal", "dim", "bold", "accent", "good", "warn", "bad", "heading",
            "heading2", "heading3", "code", "quote", "link", "italic", "strike",
            "kw", "str", "num", "com", "fn", "op",
        }
        assert expected <= set(ANSI_FOR_STYLE)

    def test_paint_span_accepts_hex_styles(self):
        out = paint_span("agent", "#9D4EDD", enabled=True)
        assert "\033[" in out and "agent" in ANSI.sub("", out)

    def test_unknown_style_is_left_alone(self):
        assert paint_span("x", "not-a-style", enabled=True) == "x"

    def test_code_fence_gets_token_colours(self):
        out = render_markdown_ansi("```python\nreturn 42\n```", 50, enabled=True)
        assert out.count("\033[") > 4
