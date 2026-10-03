#!/usr/bin/env python3
"""Tests for the gradient engine, the charts and the `xli demo` renderer.

This file used to end in ``assert False`` so pytest would print the demo output
in the traceback. That made the suite permanently red, which is the same as
having no tests at all: a real regression was indistinguishable from the demo
"failing" on purpose.

The demo is now a command (`xli demo`), and these are the assertions it never
had: the engine produces colour when asked, produces *no* colour when not, the
widths are right, and the charts are monotonic in the way their names promise.
"""

import re

from xli.ui import charts
from xli.ui.demo import (
    SAMPLE_MARKDOWN,
    animation_frames,
    render_agents_section,
    render_banner,
    render_demo,
    render_graph_section,
    render_markdown_section,
    render_report_section,
    render_syntax_section,
    sections,
)
from xli.ui.gradient import (
    ACCENT_GLOW,
    XLI_DARK_VIOLET,
    XLI_NEON_MAGENTA,
    color_depth,
    gradient_rule,
    gradient_text,
    hex_to_rgb,
    interpolate_color,
    mix,
    ramp,
    rgb_to_hex,
    visible_width,
)

ANSI = re.compile(r"\033\[[0-9;]*m")


class TestColourMath:
    def test_hex_round_trip(self):
        assert hex_to_rgb("#9D4EDD") == (157, 78, 221)
        assert rgb_to_hex((157, 78, 221)) == "#9D4EDD"

    def test_hex_without_hash(self):
        assert hex_to_rgb("5A189A") == XLI_DARK_VIOLET

    def test_interpolate_hits_both_ends(self):
        assert interpolate_color(XLI_DARK_VIOLET, XLI_NEON_MAGENTA, 0.0) == XLI_DARK_VIOLET
        assert interpolate_color(XLI_DARK_VIOLET, XLI_NEON_MAGENTA, 1.0) == XLI_NEON_MAGENTA

    def test_interpolate_clamps_out_of_range(self):
        assert interpolate_color(XLI_DARK_VIOLET, XLI_NEON_MAGENTA, -5.0) == XLI_DARK_VIOLET
        assert interpolate_color(XLI_DARK_VIOLET, XLI_NEON_MAGENTA, 5.0) == XLI_NEON_MAGENTA

    def test_mix_walks_a_multi_stop_ramp(self):
        stops = ((0, 0, 0), (128, 0, 0), (255, 0, 0))
        assert mix(*stops, t=0.0) == (0, 0, 0)
        assert mix(*stops, t=0.5) == (128, 0, 0)
        assert mix(*stops, t=1.0) == (255, 0, 0)

    def test_ramp_length_and_order(self):
        colours = ramp(list(ACCENT_GLOW.stops), 5)
        assert len(colours) == 5
        assert colours[0] == ACCENT_GLOW.stops[0]
        assert colours[-1] == ACCENT_GLOW.stops[-1]


class TestGradientText:
    def test_colour_when_enabled(self):
        out = gradient_text("привет", enabled=True)
        assert "\033[" in out
        assert visible_width(out) == 6

    def test_plain_when_disabled(self):
        assert gradient_text("привет", enabled=False) == "привет"

    def test_newlines_do_not_get_colour_codes(self):
        out = gradient_text("a\nb", enabled=True)
        assert "\n" in out
        assert visible_width(out) == 2

    def test_tick_changes_the_ramp(self):
        first = gradient_text("XLI" * 4, enabled=True, tick=0)
        later = gradient_text("XLI" * 4, enabled=True, tick=7)
        assert first != later, "animation must move, otherwise it is decoration"

    def test_no_trailing_escape_when_reset_disabled(self):
        out = gradient_text("abc", enabled=True, reset=False)
        assert not out.endswith("\033[0m")


class TestGradientRule:
    def test_width_is_exact(self):
        assert visible_width(gradient_rule(40, enabled=True)) == 40

    def test_plain_when_disabled(self):
        assert gradient_rule(10, enabled=False) == "─" * 10

    def test_zero_width_is_empty(self):
        assert gradient_rule(0, enabled=True) == ""

    def test_moves_with_tick(self):
        assert gradient_rule(30, tick=0) != gradient_rule(30, tick=9)


class TestColourDepth:
    def test_depth_is_one_of_the_known_values(self):
        assert color_depth() in ("truecolor", "256", "16", "none")

    def test_no_color_env_wins(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        assert color_depth() == "none"

    def test_colourterm_truecolor(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("COLORTERM", "truecolor")
        monkeypatch.setenv("TERM", "xterm-256color")
        assert color_depth() == "truecolor"

    def test_term_256(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("COLORTERM", raising=False)
        monkeypatch.setenv("TERM", "xterm-256color")
        assert color_depth() == "256"


class TestCharts:
    def test_sparkline_uses_all_eight_blocks(self):
        out = charts.sparkline([0, 1, 2, 3, 4, 5, 6, 7])
        assert out == charts.BLOCKS

    def test_sparkline_of_a_flat_series_is_not_blank(self):
        out = charts.sparkline([5, 5, 5, 5])
        assert out and set(out) == {charts.BLOCKS[3]}

    def test_sparkline_width_is_respected(self):
        assert len(charts.sparkline(range(100), width=20)) == 20

    def test_sparkline_empty(self):
        assert charts.sparkline([]) == ""

    def test_bars_are_sorted_by_size(self):
        lines = charts.bars([("a", 1), ("b", 10)])
        assert lines[1].count(charts.BAR_FULL) > lines[0].count(charts.BAR_FULL)

    def test_bars_handle_zero(self):
        lines = charts.bars([("a", 0)])
        assert lines and charts.BAR_FULL not in lines[0]

    def test_histogram_row_count(self):
        assert len(charts.histogram([1, 2, 3, 4, 5], buckets=4)) == 4

    def test_braille_plot_shape(self):
        art = charts.braille_plot([1, 5, 2, 8, 3], width=10, height=4)
        assert len(art) == 4
        assert all(len(line) == 10 for line in art)
        assert any(ch != " " for line in art for ch in line)

    def test_braille_plot_marks_high_points_higher(self):
        art = charts.braille_plot([0, 10], width=4, height=4)
        top_row_has_dots = any(ch != " " for ch in art[0])
        assert top_row_has_dots, "the maximum belongs in the top row"

    def test_timeline_is_proportional(self):
        lines = charts.timeline([("fast", 1), ("slow", 9)], width=40)
        assert lines[1].count("█") > lines[0].count("█")

    def test_tree_indents_by_depth(self):
        lines = charts.tree([("xli", 0), ("reviewer", 1), ("read", 2)])
        assert lines[1].startswith("│  ├─ reviewer")
        assert lines[2].startswith("│  │  ├─ read")


class TestDemoSections:
    def test_every_section_renders_something(self):
        for name in sections():
            assert render_demo(70, only=name, enabled=True).strip(), name

    def test_full_demo_mentions_its_parts(self):
        out = ANSI.sub("", render_demo(70, enabled=False))
        for word in ("X L I", "render engine", "sparkline", "reviewer", "run"):
            assert word in out

    def test_no_colour_means_no_escapes(self):
        assert "\033[" not in render_demo(70, enabled=False)

    def test_markdown_section_loses_no_text_with_width(self):
        plain = ANSI.sub("", render_markdown_section(60, enabled=False))
        assert "XLI render engine" in plain
        assert "sparkline" in plain

    def test_sample_markdown_covers_every_block_kind(self):
        from xli.ui.markdown import parse

        kinds = {block.kind for block in parse(SAMPLE_MARKDOWN)}
        assert {"heading", "paragraph", "list", "code", "quote", "rule", "table"} <= kinds

    def test_banner_width_is_stable_across_ticks(self):
        widths = {
            max(visible_width(line) for line in render_banner(70, tick=tick, enabled=True).split("\n"))
            for tick in range(8)
        }
        assert widths == {70}

    def test_agent_section_shows_every_agent(self):
        out = ANSI.sub("", render_agents_section(enabled=False))
        for name in ("xli", "reviewer", "test-writer"):
            assert name in out

    def test_report_section_has_the_numbers(self):
        out = ANSI.sub("", render_report_section(enabled=False))
        assert "steps 9/24" in out and "bash" in out

    def test_graph_section_has_a_root_and_leaves(self):
        out = ANSI.sub("", render_graph_section(enabled=False))
        assert "xli.cli" in out and "xli.ui.syntax" in out

    def test_syntax_section_highlights_keywords(self):
        from xli.ui.syntax import highlight_line

        styles = {style for _, style in highlight_line("def f():", "python")}
        assert "kw" in styles


class TestAnimation:
    def test_frames_are_all_different(self):
        frames = animation_frames(6, 60)
        assert len(set(frames)) > 1

    def test_frame_width_never_grows(self):
        for frame in animation_frames(4, 60):
            assert max(visible_width(line) for line in frame.split("\n")) <= 60
