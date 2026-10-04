"""The Neovim plugin, checked from Python.

There is no Lua interpreter in CI, so these are static checks over the plugin
sources. They are deliberately about the things that break silently:

* a command whose handler method does not exist — `:XliGraph` that does
  nothing at all, with no error anywhere,
* a highlight group referenced but never defined — a line painted in the
  terminal's default colour, which reads as "unstyled" not "broken",
* an English label in a Russian interface, or an emoji where the project
  allows only text glyphs,
* a markdown renderer that quietly stopped stripping its markers.

Behavioural tests for the plugin would need `nvim --headless`; when that is
available, `test_nvim_headless.py` skips itself rather than pretending.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "xli" / "nvim" / "plugin_root"
INIT = PLUGIN / "lua" / "xli" / "init.lua"
UI = PLUGIN / "lua" / "xli" / "ui.lua"
ENTRY = PLUGIN / "plugin" / "xli.lua"

LUA_FILES = (INIT, UI, ENTRY, PLUGIN / "lua" / "xli" / "rpc.lua")

CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def init_source() -> str:
    return read(INIT)


@pytest.fixture(scope="module")
def ui_source() -> str:
    return read(UI)


class TestCommands:
    def test_the_entry_point_registers_the_same_commands_as_setup(self):
        """A command defined only in one place vanishes after `setup()`."""
        entry = set(re.findall(r'\{\s*"(Xli\w+)"', read(ENTRY)))
        registered = set(re.findall(r'command\("(Xli\w+)"', read(INIT)))
        assert entry, "no commands found in the lazy loader"
        assert entry == registered, (
            f"only in plugin/: {sorted(entry - registered)}; "
            f"only in init.lua: {sorted(registered - entry)}"
        )

    def test_every_command_handler_exists(self):
        """`:XliGraph` calling a missing method fails silently in nvim."""
        methods = set(re.findall(r"^function M\.(\w+)", read(INIT), re.MULTILINE))
        referenced = set(re.findall(r'\{\s*"Xli\w+",\s*"(\w+)"', read(ENTRY)))
        missing = sorted(referenced - methods)
        assert not missing, f"commands point at missing methods: {missing}"

    def test_every_command_description_is_russian(self):
        descriptions = []
        for path in (ENTRY, INIT):
            for line in read(path).splitlines():
                # The lazy loader builds a default from the command name; that
                # template is the only description allowed to be Latin.
                if "opts.desc = " in line:
                    continue
                descriptions += re.findall(r'desc\s*=\s*"([^"]*)"', line)
        assert descriptions, "no command descriptions found"
        english = [text for text in descriptions if not CYRILLIC.search(text)]
        assert not english, f"these descriptions are not Russian: {english}"

    def test_the_handler_table_matches_the_documented_commands(self):
        """README lists the commands; a new one that is not documented is a
        command nobody knows about."""
        readme = read(ROOT / "README.md")
        names = re.findall(r'"(Xli\w+)"', read(ENTRY))
        undocumented = sorted({name for name in names if name not in readme})
        assert not undocumented, f"commands missing from README: {undocumented}"


class TestInterfaceLanguage:
    def test_no_emoji_or_pictographs(self):
        """Text glyphs only. Anything above U+2BFF in these files is a drawing."""
        for path in LUA_FILES:
            for line_no, line in enumerate(read(path).splitlines(), start=1):
                bad = [char for char in line if ord(char) > 0x2BFF]
                assert not bad, f"{path.name}:{line_no} has {bad}"

    def test_notifications_are_russian(self):
        for path in (INIT, ENTRY):
            for match in re.finditer(r'vim\.notify\("([^"]*)"(\s*\.\.)?', read(path)):
                message, concatenated = match.group(1), match.group(2)
                # `"[xli] " .. tostring(message)` is a prefix; the message that
                # follows it is built by a Russian error path (checked below).
                if concatenated:
                    continue
                assert CYRILLIC.search(message), f"{path.name}: {message!r}"

    def test_errors_reported_to_the_user_are_russian(self, init_source):
        errors = re.findall(r'M\._error\("([^"]*)"', init_source)
        assert errors, "no user-facing errors found — did the calls change shape?"
        english = [text for text in errors if not CYRILLIC.search(text)]
        assert not english, f"these errors are not Russian: {english}"

    def test_the_status_line_speaks_russian(self, init_source):
        for word in ("работаю", "готово", "шаг", "инструмент"):
            assert word in init_source, f"the status line never says {word!r}"


class TestRendering:
    def test_the_markdown_renderer_strips_its_markers(self, ui_source):
        """Leftover `**` and `==` in a chat window are noise, not emphasis."""
        for marker in ('gsub("%*%*', 'gsub("==', 'gsub("~~', 'gsub("`'):
            assert marker in ui_source, f"inline markdown does not handle {marker}"

    def test_the_markdown_renderer_knows_headings_lists_and_fences(self, ui_source):
        assert '"XliH" .. level' in ui_source
        assert '"• "' in ui_source
        assert "╭─" in ui_source and "╰─" in ui_source
        assert "▎ " in ui_source

    def test_links_keep_only_their_label(self, ui_source):
        """Same trade as the CLI: a URL doubles the width of every citation."""
        assert '%[([^%]]*)%]%b()' in ui_source

    def test_highlight_groups_used_are_defined(self):
        defined = set(re.findall(r"^\s*(Xli\w+)\s*=\s*\{", read(UI), re.MULTILINE))
        assert defined, "no highlight groups defined"
        # `XliTools` and friends are commands, not colours; the level suffix in
        # `"XliH" .. level` is built at runtime.
        commands = set(re.findall(r'"(Xli\w+)"', read(ENTRY)))
        used: set[str] = set()
        for path in (INIT, UI):
            used.update(re.findall(r'"(Xli[A-Za-z0-9]+)"', read(path)))
        missing = sorted(used - defined - commands - {"XliH"})
        assert not missing, f"referenced but never defined: {missing}"

    def test_the_palette_is_violet_not_the_editor_default(self):
        """XLI is purple; a plugin that looks like generic vim looks accidental."""
        body = read(UI)
        violet = re.findall(r'"(#a679ff|#c4a7ff|#b18cff|#d7b3ff|#8a86a8)"', body)
        assert len(violet) >= 4, "the palette lost its violet accent"


class TestAnimation:
    def test_the_spinner_matches_the_terminal_one(self, init_source):
        """The same frames in nvim and in the TUI: one product, one motion."""
        frames = re.findall(r'"([⣾⣽⣻⢿⡿⣟⣯⣷])"', init_source)
        assert frames == list("⣾⣽⣻⢿⡿⣟⣯⣷")

    def test_the_animation_is_opt_out(self, init_source):
        assert "spinner = true" in init_source
        assert "config.spinner" in init_source

    def test_the_window_title_says_what_is_happening(self, init_source):
        assert "работаю" in init_source and "set_title" in init_source


class TestToolGlyphs:
    def test_call_lines_use_a_glyph_per_tool(self, init_source):
        table = re.search(r"local GLYPH = \{(.*?)\n\}", init_source, re.DOTALL)
        assert table, "no glyph table"
        entries = re.findall(r"(\w+)\s*=\s*\"", table.group(1))
        assert len(entries) >= 15

    def test_the_glyph_table_covers_tools_that_exist(self):
        """A glyph for a tool that does not exist is a typo waiting to mislead
        a reader; every key must be a real tool name."""
        from xli.tools.registry import default_registry

        names = set(default_registry().names())
        entries = set(
            re.findall(r"(\w+)\s*=\s*\"", re.search(r"local GLYPH = \{(.*?)\n\}", read(INIT), re.DOTALL).group(1))
        )
        # `think` and friends are all real; allow the few aliases the plugin
        # uses for tools that live behind MCP.
        unknown = sorted(entries - names - {"file", "file_path"})
        assert not unknown, f"glyph entries for tools that do not exist: {unknown}"

    def test_a_call_with_no_arguments_still_has_a_name(self, init_source):
        assert 'or "?"' in init_source
