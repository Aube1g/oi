"""Tests for the display-width model and the dependency-free markdown renderer.

These two modules are the foundation of the TUI's text handling, so the tests
deliberately use real Unicode rather than ASCII: the whole point is that a
character is not always a cell, and that Markdown has to render without rich.
"""

from xli.ui.markdown import (
    inline_spans,
    parse,
    render_plain,
    render_rows,
    strip_inline,
    wrap_row,
)
from xli.ui.text import (
    char_width,
    display_width,
    pad,
    split_cells,
    truncate,
)

# ---------------------------------------------------------------- display width


class TestCharWidth:
    def test_ascii_is_one(self):
        assert char_width("a") == 1

    def test_combining_mark_is_zero(self):
        # U+0301 combining acute adds no cell of its own.
        assert char_width("\u0301") == 0

    def test_hangul_jamo_medium_is_zero(self):
        assert char_width("\u1160") == 0

    def test_cjk_ideograph_is_two(self):
        assert char_width("漢") == 2

    def test_hangul_syllable_is_two(self):
        assert char_width("한") == 2

    def test_fullwidth_latin_is_two(self):
        assert char_width("Ａ") == 2

    def test_emoji_is_two(self):
        assert char_width("🎉") == 2

    def test_cyrillic_is_one(self):
        # The user's language: narrow, one cell each.
        assert char_width("п") == 1


class TestDisplayWidth:
    def test_ascii_equals_len(self):
        assert display_width("hello") == len("hello")

    def test_cyrillic_equals_len(self):
        assert display_width("привет") == 6 == len("привет")

    def test_cjk_is_double_len(self):
        text = "日本語"
        assert len(text) == 3
        assert display_width(text) == 6

    def test_emoji_counts_two_cells(self):
        text = "🎉"
        assert len(text) == 1
        assert display_width(text) == 2

    def test_combining_marks_do_not_add_cells(self):
        # "e" + combining acute renders as one glyph.
        assert display_width("e\u0301") == 1

    def test_mixed_script(self):
        assert display_width("ok 漢字") == 2 + 1 + 4

    def test_empty_is_zero(self):
        assert display_width("") == 0


class TestTruncate:
    def test_short_text_unchanged(self):
        assert truncate("привет", 10) == "привет"

    def test_exact_fit_unchanged(self):
        assert truncate("привет", 6) == "привет"

    def test_truncates_to_cells_with_ellipsis(self):
        assert truncate("привет мир", 8) == "привет …"

    def test_cjk_truncates_on_cell_boundary(self):
        assert truncate("日本語です", 5) == "日本…"

    def test_never_exceeds_width(self):
        for text in ("привет мир", "日本語です", "🎉🎉🎉🎉", "a" * 40):
            for width in range(1, 20):
                assert display_width(truncate(text, width)) <= width

    def test_zero_width_yields_empty(self):
        assert truncate("привет", 0) == ""

    def test_width_one_yields_ellipsis(self):
        assert truncate("привет", 1) == "…"


class TestPad:
    def test_pads_to_exact_cells(self):
        assert display_width(pad("привет", 10)) == 10

    def test_cjk_pads_to_exact_cells(self):
        assert display_width(pad("日本語", 10)) == 10

    def test_right_alignment(self):
        assert pad("ab", 5, "right") == "   ab"

    def test_center_alignment(self):
        assert pad("ab", 6, "center") == "  ab  "

    def test_never_exceeds_width(self):
        for text in ("привет", "日本語", "🎉🎉"):
            for width in range(0, 12):
                assert display_width(pad(text, width)) <= width


class TestSplitCells:
    def test_ascii_splits_on_space(self):
        # "hello " is exactly 6 cells, so the space stays with the first chunk
        # and the break lands on the word boundary.
        assert split_cells("hello world", 6) == ["hello ", "world"]

    def test_cyrillic_splits_on_space(self):
        assert split_cells("привет мир", 6) == ["привет", " мир"]

    def test_cjk_splits_on_cell_boundary(self):
        # Each ideograph is 2 cells, so a 3-cell budget takes one at a time
        # rather than splitting a glyph in half.
        assert split_cells("日本語", 3) == ["日", "本", "語"]
        assert split_cells("日本語", 4) == ["日本", "語"]

    def test_emoji_is_not_split(self):
        # Two cells each; a width of 3 cannot take two of them.
        assert split_cells("🎉🎉", 3) == ["🎉", "🎉"]

    def test_width_zero_yields_empty(self):
        assert split_cells("hello", 0) == []


# ---------------------------------------------------------------- markdown parse


class TestParseBlocks:
    def test_kinds_in_order(self):
        doc = "# H\n\ntext\n\n- a\n\n```\nx\n```\n\n> q\n\n---\n"
        kinds = [b.kind for b in parse(doc)]
        assert kinds == [
            "heading", "paragraph", "list", "code", "quote", "rule",
        ]

    def test_heading_level_and_inline(self):
        block = parse("# Заголовок")[0]
        assert block.level == 1
        assert block.inline == [("Заголовок", "normal")]

    def test_fenced_code_keeps_language(self):
        block = parse("```python\nprint(1)\n```")[0]
        assert block.kind == "code"
        assert block.lang == "python"
        assert block.lines == ["print(1)"]

    def test_unterminated_fence_extends_to_end(self):
        # The agent gets cut off mid-code all the time; the parser must not lose it.
        block = parse("```py\nprint(1)")[0]
        assert block.kind == "code"
        assert block.lines == ["print(1)"]

    def test_ordered_list_keeps_number(self):
        block = parse("1. one\n2. two")[0]
        assert [item["number"] for item in block.items] == [1, 2]

    def test_task_list_checked_state(self):
        block = parse("- [x] done\n- [ ] open")[0]
        assert [item["checked"] for item in block.items] == [True, False]

    def test_table_cells_and_header_flag(self):
        doc = "| A | B |\n| --- | --- |\n| 1 | 2 |"
        block = parse(doc)[0]
        assert block.kind == "table"
        assert block.header
        assert block.rows[0] == ["A", "B"]
        assert block.rows[1] == ["1", "2"]

    def test_single_dash_separator_is_a_table(self):
        # GFM allows one dash per column; requiring two lost these tables.
        assert parse("| A | B |\n| - | - |\n| 1 | 2 |")[0].kind == "table"

    def test_table_column_count_is_evened_out(self):
        block = parse("| A | B | C |\n| --- |\n| 1 |")[0]
        assert all(len(row) == 3 for row in block.rows)

    def test_setext_heading(self):
        assert parse("Title\n=====")[0].level == 1
        assert parse("Title\n-----")[0].level == 2

    def test_empty_document_parses_to_nothing(self):
        assert parse("") == []
        assert parse("\n\n   \n") == []


class TestInline:
    def test_bold(self):
        assert ("text", "bold") in inline_spans("**text**")

    def test_italic_with_asterisk(self):
        assert ("text", "italic") in inline_spans("*text*")

    def test_italic_with_underscore(self):
        assert ("text", "italic") in inline_spans("_text_")

    def test_strikethrough(self):
        assert ("gone", "strike") in inline_spans("~~gone~~")

    def test_inline_code(self):
        spans = inline_spans("use `code` here")
        assert ("code", "code") in spans

    def test_link_shows_label_only(self):
        spans = inline_spans("see [docs](https://xkiro.com)")
        joined = "".join(t for t, _ in spans)
        assert "docs" in joined
        assert "https://xkiro.com" not in joined
        assert ("docs", "link") in spans

    def test_autolink(self):
        assert ("https://xkiro.com", "link") in inline_spans("see https://xkiro.com")

    def test_bare_www_is_not_a_link(self):
        # Guarding the false positive this parser must not produce.
        assert all(style != "link" for _, style in inline_spans("www is a prefix"))

    def test_url_before_word_is_not_a_link(self):
        assert all(style != "link" for _, style in inline_spans("ahttp://x.com"))

    def test_nested_bold_inside_italic(self):
        spans = inline_spans("*a **b** c*")
        assert ("b", "bold") in spans

    def test_unmatched_marker_is_literal(self):
        # Unbalanced markup must not swallow the rest of the line.
        assert ("2 * 3 * 4", "normal") in inline_spans("2 * 3 * 4")

    def test_cyrillic_emphasis(self):
        assert ("привет", "bold") in inline_spans("**привет**")

    def test_empty_inline_is_empty(self):
        assert inline_spans("") == []


class TestStripInline:
    def test_removes_markup(self):
        assert strip_inline("**bold** and `code`") == "bold and code"

    def test_link_becomes_label(self):
        assert strip_inline("[docs](https://x.com)") == "docs"

    def test_plain_text_unchanged(self):
        assert strip_inline("привет мир") == "привет мир"


# ---------------------------------------------------------------- markdown render


class TestRenderRows:
    def test_heading_is_marked(self):
        rows = render_rows("# Заголовок", 60)
        assert any(style == "heading" for row in rows for _, style in row)

    def test_code_block_is_boxed(self):
        flat = "".join(t for row in render_rows("```py\nx = 1\n```", 40) for t, _ in row)
        assert "┌─ py" in flat
        assert "x = 1" in flat
        assert "└" in flat

    def test_quote_is_marked(self):
        rows = render_rows("> цитата", 40)
        assert any(style == "quote" for row in rows for _, style in row)

    def test_table_draws_borders(self):
        flat = "".join(
            t for row in render_rows("| A | B |\n| - | - |\n| 1 | 2 |", 40) for t, _ in row
        )
        assert "┌" in flat and "┼" in flat

    def test_table_column_widths_follow_display_width(self):
        # A CJK cell occupies two cells; the column must widen accordingly.
        flat = "".join(
            t for row in render_rows("| Имя | 名前 |\n| - | - |\n| а | 漢 |", 40)
            for t, _ in row
        )
        assert "Имя" in flat and "名前" in flat and "漢" in flat

    def test_rule_is_drawn(self):
        flat = "".join(t for row in render_rows("---", 30) for t, _ in row)
        assert "─" in flat

    def test_list_items_are_bulleted(self):
        flat = "".join(t for row in render_rows("- a\n- b", 30) for t, _ in row)
        assert flat.count("•") == 2

    def test_rows_never_exceed_width(self):
        doc = (
            "# Заголовок\n\n" + "слово " * 40 + "\n\n"
            "```python\nprint('привет')\n```\n\n"
            "| Модель | Контекст |\n| --- | --- |\n| qwen3 | 128K |\n"
        )
        for width in (20, 40, 60, 80):
            for row in render_rows(doc, width):
                assert display_width("".join(t for t, _ in row)) <= width

    def test_wide_glyphs_do_not_break_the_frame(self):
        for row in render_rows("日本語のテキスト 🎉", 20):
            assert display_width("".join(t for t, _ in row)) <= 20

    def test_empty_document_renders_nothing(self):
        assert render_rows("", 60) == []


class TestRenderPlain:
    def test_strips_markup(self):
        assert "**bold**" not in render_plain("# H\n\n**bold** text", 60)

    def test_preserves_content(self):
        text = render_plain("# H\n\n**bold** text", 60)
        assert "H" in text and "bold" in text and "text" in text


class TestWrapRow:
    def test_wraps_long_spans(self):
        rows = wrap_row([("a" * 40, "normal")], 10)
        assert len(rows) > 1
        assert all(display_width("".join(t for t, _ in r)) <= 10 for r in rows)

    def test_preserves_style_across_wrap(self):
        rows = wrap_row([("word " * 10, "bold")], 12)
        assert all(style == "bold" for row in rows for _, style in row if _[0].strip())

    def test_empty_row_yields_empty_row(self):
        assert wrap_row([], 10) == [[]]

    def test_wide_glyphs_respect_the_width(self):
        rows = wrap_row([("日本語" * 10, "normal")], 8)
        assert all(display_width("".join(t for t, _ in r)) <= 8 for r in rows)
