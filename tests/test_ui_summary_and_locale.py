"""Contracts for the shared UI vocabulary: call summaries, the locale table
and the ANSI markdown renderer the CLI and REPL print through."""

from __future__ import annotations

import pytest

from xli.ui import locale
import re

from xli.ui.ansi import render_markdown_ansi
from xli.ui.summary import summarise_call


class TestSummariseCall:
    def test_read_shows_path_offset_and_limit(self):
        assert summarise_call("read", {"path": "a.py"}) == "a.py"
        assert summarise_call("read", {"path": "a.py", "offset": 40, "limit": 20}) == "a.py c 40 +20"

    def test_write_shows_path_only(self):
        out = summarise_call("write", {"path": "a.py", "content": "y" * 900})
        assert out == "a.py"
        assert "yyy" not in out

    def test_write_without_path_previews_first_line(self):
        out = summarise_call("write", {"content": "x" * 500})
        assert out.startswith("xxx") and out.endswith("…")
        assert len(out) < 500

    def test_grep_quotes_the_pattern(self):
        assert summarise_call("grep", {"pattern": "TODO"}) == "'TODO'"
        assert summarise_call("grep", {"pattern": "TODO", "path": "src"}) == "'TODO' в src"

    def test_bash_and_long_commands_truncate(self):
        out = summarise_call("bash", {"command": "echo " + "a" * 500})
        assert len(out) <= 80 and out.endswith("…")

    def test_think_summarises_the_thought(self):
        out = summarise_call("think", {"thought": "  расклад  "})
        assert "расклад" in out

    def test_unknown_tool_falls_back_to_compact_json(self):
        out = summarise_call("mystery", {"a": 1})
        assert '"a":1' in out

    def test_none_args_do_not_crash(self):
        assert summarise_call("ls", None) == "."


class TestLocale:
    def test_t_format_and_fallback(self):
        locale.set_lang("ru")
        try:
            assert locale.t("ok") == "ок"
            assert locale.t("step", index=1, max_steps=9) == "-- шаг 1/9"
        finally:
            locale.set_lang("en")
        assert locale.t("ok") == "ok"
        # A missing key degrades to the key itself, never an exception.
        assert locale.t("definitely_not_a_key") == "definitely_not_a_key"

    def test_missing_kwargs_keeps_template(self):
        assert locale.t("step") == locale.t("step")

    @pytest.mark.parametrize("message", [
        'HTTP 402 {"error":{"message":"insufficient_balance"}}',
        "Request timed out after 120.0s",
        "HTTP 429 too many requests",
        "HTTP 401 bad api key",
        "connection refused by the gateway",
    ])
    def test_humanise_recognises_gateway_shapes(self, message):
        out = locale.humanise_provider_error(message)
        assert "{" not in out  # never a raw JSON wall

    def test_humanise_keeps_the_timeout_seconds(self):
        assert "120.0" in locale.humanise_provider_error("Request timed out after 120.0s")

    def test_humanise_limits_unknown_errors(self):
        out = locale.humanise_provider_error("z" * 5000)
        assert len(out) < 400

    def test_english_table_has_every_russian_key_and_back(self):
        assert set(locale._RU) == set(locale._EN)


class TestRenderMarkdownAnsi:
    def test_plain_mode_has_no_escape_codes(self):
        out = render_markdown_ansi("# Title\n\ntext `code`", width=60, enabled=False)
        assert "\033[" not in out
        assert "Title" in out and "code" in out

    def test_colour_mode_wraps_styles(self):
        out = render_markdown_ansi("# Title", width=60, enabled=True)
        assert "\033[" in out and "Title" in re.sub(r"\033\[[0-9;]*m", "", out)

    def test_heading_levels_are_visually_distinct(self):
        """Each heading level must paint differently, glow or not.

        Headings are painted per character along a gradient now, so the old
        "one escape code per level" assertion no longer describes the output.
        What must hold is the property it was protecting: three levels, three
        different renderings.
        """
        import re as _re

        levels = [
            render_markdown_ansi("# a", width=60, enabled=True),
            render_markdown_ansi("## b", width=60, enabled=True),
            render_markdown_ansi("### c", width=60, enabled=True),
        ]
        assert len({_re.sub(r"\033\[[0-9;]*m", "", line) for line in levels}) == 3
        assert len(set(levels)) == 3, "levels must not share a palette"
        # And the marker glyph still says which level it is.
        assert "▌" in levels[0] and "▐" in levels[1] and "│" in levels[2]


class TestGuides:
    def test_list_and_read(self):
        from xli.guides import list_guides, read_guide

        names = [stem for stem, _title in list_guides()]
        assert "quickstart" in names and "troubleshooting" in names
        assert read_guide("quickstart").startswith("# ")
        assert read_guide("nope") is None
