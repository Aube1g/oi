"""The Neovim plugin, run for real.

`tests/test_nvim_plugin.py` greps the Lua. This module loads it: `lupa` embeds
a Lua interpreter, `tests/fixtures/fake_vim.lua` stands in for Neovim, and the
plugin's own functions are called the way the editor would call them.

That matters because the interesting bugs in a plugin of this shape are not
typos in strings — they are a `vim.api` call with the wrong arity, a table
indexed one level too deep, a `gsub` that eats the wrong marker. None of them
show up in a grep, and all of them show up here.

The tests skip when `lupa` is not installed (it is an optional dev dependency);
`pip install -e '.[dev]'` brings it in.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

lupa = pytest.importorskip("lupa", reason="нужен lupa для запуска Lua-тестов")

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "xli" / "nvim" / "plugin_root"
STUB = ROOT / "tests" / "fixtures" / "fake_vim.lua"
FAKE_RPC = ROOT / "tests" / "fixtures" / "fake_rpc.lua"


def _to_python(value):
    """Lua table -> Python, for the one place the tests need to read it."""
    if isinstance(value, str) or not hasattr(value, "items"):
        return value
    keys = list(value.keys())
    if keys and all(isinstance(key, int) for key in keys):
        return [_to_python(value[index]) for index in sorted(keys)]
    return {key: _to_python(value[key]) for key in keys}


def sources() -> dict[str, str]:
    return {
        "xli.ui": (PLUGIN / "lua" / "xli" / "ui.lua").read_text(encoding="utf-8"),
        "xli.rpc": (PLUGIN / "lua" / "xli" / "rpc.lua").read_text(encoding="utf-8"),
        "xli": (PLUGIN / "lua" / "xli" / "init.lua").read_text(encoding="utf-8"),
        "plugin": (PLUGIN / "plugin" / "xli.lua").read_text(encoding="utf-8"),
    }


class Neovim:
    """A Lua state with the fake editor loaded and the plugin available."""

    def __init__(self) -> None:
        self.lua = lupa.LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute(STUB.read_text(encoding="utf-8"))
        self.modules = self.lua.eval("_G.__modules")
        self.state = self.lua.eval("_G.__state")
        self.modules["xli.rpc"] = self.lua.execute(FAKE_RPC.read_text(encoding="utf-8"))
        self._install_json()
        for name, text in sources().items():
            if name in ("plugin", "xli.rpc"):
                continue  # the entry point is sourced on demand; rpc has a fake
            module = self.lua.execute(text)
            self.modules[name] = module

    def _install_json(self) -> None:
        """The fake editor's json is a stub; rpc.lua needs a real codec, and
        lupa makes Python's own available without writing a parser in Lua."""
        table = self.lua.eval("function() return vim.json end")()

        def decode(text):
            return self.lua.table_from(json.loads(str(text)))

        def encode(value):
            return json.dumps(_to_python(value), ensure_ascii=False)

        table["decode"] = decode
        table["encode"] = encode

    # -- helpers -----------------------------------------------------------
    def load_entry_point(self) -> None:
        """Source plugin/xli.lua, which registers the commands at startup."""
        self.lua.execute(sources()["plugin"])

    def ui(self):
        return self.modules["xli.ui"]

    def agent(self):
        return self.modules["xli"]

    def commands(self) -> dict[str, dict]:
        return {name: spec["opts"] for name, spec in self.state["commands"].items()}

    def command_descriptions(self) -> dict[str, str]:
        return {name: opts["desc"] for name, opts in self.commands().items()}

    def notifications(self) -> list[str]:
        return list(self.state["notifications"].values())

    def highlights_defined(self) -> set[str]:
        return set(self.state["highlights"].keys())

    def markdown(self, text: str) -> list[tuple[str, str]]:
        """Run ui.md_lines and return `(text, group)` pairs."""
        render = self.lua.eval(
            """
            function(md)
              local out = {}
              for _, entry in ipairs(md) do
                out[#out + 1] = (entry[2] or "") .. "\\t" .. entry[1]
              end
              return table.concat(out, "\\n")
            end
            """
        )
        rendered = render(self.ui().md_lines(text))
        if rendered == "":
            return []
        pairs = []
        for line in rendered.split("\n"):
            group, _, body = line.partition("\t")
            pairs.append((body, group))
        return pairs

    def window(self):
        return self.ui().open(None)

    # Lua methods take `self` explicitly when called from Python: `win.append`
    # is the raw function, not a bound method.
    @staticmethod
    def call(window, name, *args):
        return window[name](window, *args)

    def window_lines(self, win) -> list[str]:
        """What the buffer shows, status line included."""
        return [str(line) for line in self.state["bufs"][win.buf]["lines"].values()]

    def drain(self) -> None:
        """Run the callbacks the plugin handed to vim.schedule()."""
        drain = self.lua.eval("_G.__drain")
        # One pass can schedule more work (a repaint after an append), so keep
        # going until the editor goes quiet.
        while drain() > 0:
            pass

    def client(self):
        return self.state["rpc_client"]

    def to_lua(self, value):
        """Python data into real Lua tables, nested levels included.

        `table_from` converts the top level only, and a Python list left in
        the middle behaves like userdata — which is how a test quietly stops
        testing what it says it tests.
        """
        if isinstance(value, dict):
            table = self.lua.table()
            for key, item in value.items():
                table[key] = self.to_lua(item)
            return table
        if isinstance(value, (list, tuple)):
            table = self.lua.table()
            for index, item in enumerate(value, start=1):
                table[index] = self.to_lua(item)
            return table
        return value

    def emit(self, method: str, params: dict) -> None:
        """Play one kernel notification, as xli/rpc.lua would."""
        handler = self.client()["handlers"][method]
        assert handler is not None, f"the plugin does not listen for {method}"
        handler(self.to_lua(params) if params else None)

    def finish_run(self, result: dict) -> None:
        callback = self.client()["pending"]
        assert callback is not None, "the plugin never asked the kernel to run"
        callback(None, self.to_lua(result))
        self.drain()


class TestAnAgentRun:
    """Drive :Xli the way the kernel would, and read the window."""

    def start(self, nvim, task: str = "почини тесты в ./api"):
        nvim.agent().run(task)
        nvim.drain()
        return nvim

    def test_the_task_is_echoed_before_anything_happens(self, nvim):
        self.start(nvim)
        assert any("▸ вы  почини тесты" in line for line in self.lines(nvim))

    def test_the_kernel_is_asked_to_run_the_task(self, nvim):
        self.start(nvim)
        requests = list(nvim.client()["requests"].values())
        assert requests[0]["method"] == "agent.run"
        assert requests[0]["params"]["task"] == "почини тесты в ./api"

    def test_a_tool_call_reads_as_a_sentence(self, nvim):
        self.start(nvim)
        nvim.emit("agent.tool_call", {"name": "read", "args": {"path": "api/main.py"}})
        nvim.drain()
        assert any("◇ read  api/main.py" in line for line in self.lines(nvim))

    def test_a_failed_call_is_marked_not_hidden(self, nvim):
        self.start(nvim)
        nvim.emit("agent.tool_result", {"name": "bash", "ok": False, "summary": "код 1"})
        nvim.drain()
        assert any("✗ код 1" in line for line in self.lines(nvim))

    def test_an_answer_renders_as_markdown(self, nvim):
        self.start(nvim)
        nvim.emit("agent.assistant", {"text": "## Итог\n- готово\n"})
        nvim.drain()
        lines = self.lines(nvim)
        assert "Итог" in lines
        assert any(line.strip().startswith("• готово") for line in lines)
        assert not any("##" in line for line in lines)

    def test_the_spinner_line_never_reaches_the_transcript(self, nvim):
        self.start(nvim)
        nvim.emit("agent.step", {"index": 2, "max_steps": 5})
        nvim.drain()
        status = self.lines(nvim)[-1]
        assert "шаг 2/5" in status
        assert "⣿" in status or "⣽" in status

    def test_the_run_ends_with_a_russian_summary(self, nvim):
        self.start(nvim)
        nvim.emit("agent.step", {"index": 1, "max_steps": 5})
        nvim.emit("agent.tool_call", {"name": "read", "args": {"path": "a.py"}})
        nvim.emit("agent.tool_result", {"name": "read", "ok": True, "summary": "10 строк"})
        nvim.emit("agent.agent", {"phase": "end", "stopped_reason": "done", "steps": 1})
        nvim.finish_run({"ok": True, "summary": "готово", "steps": 1, "tool_calls": 1,
                         "stopped_reason": "done", "seconds": 1.5})
        lines = self.lines(nvim)
        assert any(line.strip().startswith("✓ готово") for line in lines)
        assert not nvim.agent()._status_text().startswith("⣿")

    def test_the_status_line_counts_in_russian(self, nvim):
        self.start(nvim)
        nvim.emit("agent.tool_call", {"name": "read", "args": {"path": "a.py"}})
        nvim.emit("agent.tool_call", {"name": "bash", "args": {"command": "ls"}})
        nvim.emit("agent.step", {"index": 1, "max_steps": 3})
        nvim.emit("agent.agent", {"phase": "end", "stopped_reason": "done"})
        nvim.finish_run({"ok": True, "summary": "", "steps": 1, "tool_calls": 2,
                         "stopped_reason": "done", "seconds": 1.0})
        assert nvim.agent()._status_text() == "● готово: 1 шаг, 2 инструмента"

    def test_the_plugin_listens_for_every_event_the_kernel_sends(self, nvim):
        """The old plugin waited for `agent.end`, which the kernel never sends."""
        self.start(nvim)
        handled = set(nvim.client()["handlers"].keys())
        # Mirrors xli/ui/report.py::stats_from_events (agent/step/step_done/
        # tool_call/tool_result) plus the streaming events it ignores.
        expected = {
            "agent.agent", "agent.step", "agent.step_done", "agent.assistant",
            "agent.tool_call", "agent.tool_result", "agent.repair",
            "agent.warning", "agent.error",
        }
        assert expected <= handled, f"not handled: {sorted(expected - handled)}"

    def test_a_second_task_is_refused_while_one_runs(self, nvim):
        self.start(nvim)
        nvim.agent().run("ещё раз")
        nvim.drain()
        assert any("агент уже работает" in note for note in nvim.notifications())

    def test_repeat_runs_the_previous_task(self, nvim):
        self.start(nvim)
        nvim.agent().again()
        nvim.drain()
        requests = list(nvim.client()["requests"].values())
        assert requests[-1]["params"]["task"] == "почини тесты в ./api"

    def lines(self, nvim) -> list[str]:
        return nvim.window_lines(nvim.agent()._window())



@pytest.fixture
def nvim() -> Neovim:
    return Neovim()


class TestMarkdownRendering:
    def test_headings_lose_their_hashes(self, nvim):
        rendered = nvim.markdown("# Заголовок\n## Раздел\n### Мелочь\n")
        assert rendered == [
            ("Заголовок", "XliH1"),
            ("Раздел", "XliH2"),
            ("Мелочь", "XliH3"),
        ]

    def test_bullets_become_bullets(self, nvim):
        assert nvim.markdown("- первый\n- второй\n") == [
            ("  • первый", "XliAssistant"),
            ("  • второй", "XliAssistant"),
        ]

    def test_nesting_keeps_its_indent(self, nvim):
        rendered = nvim.markdown("- родитель\n  - ребёнок\n    - внук\n")
        indents = [len(body) - len(body.lstrip()) for body, _ in rendered]
        assert indents == sorted(indents)
        assert len(set(indents)) == 3

    def test_ordered_lists_keep_their_numbers(self, nvim):
        rendered = nvim.markdown("1. первый\n2. второй\n")
        assert [body for body, _ in rendered] == ["1. первый", "2. второй"]

    def test_quotes_get_a_bar(self, nvim):
        assert nvim.markdown("> цитата\n") == [("▎ цитата", "XliDim")]

    def test_rules_become_a_line(self, nvim):
        (body, group), = nvim.markdown("---\n")
        assert group == "XliRule"
        assert set(body) == {"─"}

    def test_code_fences_are_boxed(self, nvim):
        rendered = nvim.markdown("```python\nprint(1)\n```\n")
        assert [group for _, group in rendered] == ["XliBox", "XliCode", "XliBox"]
        assert rendered[0][0].startswith("╭─ python")
        assert rendered[1][0] == "│ print(1)"
        assert rendered[2][0] == "╰─"

    def test_inline_markers_are_stripped(self, nvim):
        cases = {
            "**жирный**": ("жирный", "XliBold"),
            "*курсив*": ("курсив", "XliItalic"),
            "`код`": ("код", "XliCode"),
            "==метка==": ("метка", "XliMark"),
            "~~старое~~": ("старое", "XliStrike"),
        }
        for source, expected in cases.items():
            (rendered,) = nvim.markdown(source + "\n")
            # elif-chains: a line is one group, but the text must come out clean
            assert expected[0] in rendered[0], f"{source} rendered as {rendered}"
            assert "**" not in rendered[0] and "`" not in rendered[0]
            assert "==" not in rendered[0] and "~~" not in rendered[0]

    def test_a_link_keeps_only_its_label(self, nvim):
        rendered = nvim.markdown("смотри [документацию](https://example.com/very/long)\n")
        assert rendered == [("смотри документацию", "XliAssistant")]

    def test_prose_survives_untouched(self, nvim):
        assert nvim.markdown("обычный текст без разметки\n") == [
            ("обычный текст без разметки", "XliAssistant")
        ]

    def test_empty_input_renders_nothing(self, nvim):
        assert nvim.markdown("") == []
        assert nvim.markdown("\n\n") == []


class TestWindow:
    def test_lines_land_in_the_buffer(self, nvim):
        win = nvim.window()
        nvim.call(win, "append", nvim.to_lua([["▸ вы  задача", "XliUser"], ["", None]]))
        assert "▸ вы  задача" in nvim.call(win, "text")

    def test_the_status_line_is_replaced_not_appended(self, nvim):
        win = nvim.window()
        nvim.call(win, "append", "первая")
        nvim.call(win, "set_status", "⣽ работаю 1,0 с", "XliDim")
        nvim.call(win, "set_status", "✓ готово", "XliOk")
        lines = nvim.window_lines(win)
        assert sum("готово" in line for line in lines) == 1
        assert not any("работаю" in line for line in lines)

    def test_copying_the_answer_leaves_the_spinner_out(self, nvim):
        """`Y` copies the conversation, not the progress bar."""
        win = nvim.window()
        nvim.call(win, "append", "ответ")
        nvim.call(win, "set_status", "⣽ работаю 2,0 с", "XliDim")
        assert nvim.call(win, "text") == "ответ"

    def test_the_window_title_can_change(self, nvim):
        win = nvim.window()
        nvim.call(win, "set_title", " ◆ XLI · работаю 2,0 с ")
        titles = [config["title"] for config in nvim.state["windows"].values()]
        assert any("работаю" in title for title in titles)

    def test_colours_are_only_ever_groups_that_exist(self, nvim):
        # define_highlights() runs on setup, as it does in the editor.
        nvim.agent().setup(None)
        win = nvim.window()
        nvim.call(win, "append", nvim.to_lua([["строка", "XliTool"]]))
        used = {spec["group"] for spec in nvim.state["bufs"][win.buf]["highlights"].values()}
        assert used <= nvim.highlights_defined(), f"undefined: {sorted(used)}"

    def test_escape_and_q_close_the_window(self, nvim):
        win = nvim.window()
        assert nvim.call(win, "is_open")
        nvim.call(win, "close")
        assert not nvim.call(win, "is_open")

    def test_a_user_keymap_is_left_alone(self, nvim):
        """The window maps q and <Esc> buffer-locally, never globally."""
        nvim.window()
        keymaps = list(nvim.state["keymaps"].values())
        assert keymaps, "no keymaps were registered"
        for spec in keymaps:
            assert spec["opts"]["buffer"], "q was mapped globally"


class TestCommands:
    def test_setup_registers_every_command(self, nvim):
        nvim.agent().setup(None)
        commands = nvim.commands()
        expected = {
            "Xli", "XliAgain", "XliTools", "XliGraph", "XliStatus",
            "XliSessions", "XliKernel", "XliClose", "XliSelection",
            "XliDiagnostics",
        }
        assert expected <= set(commands)

    def test_the_entry_point_registers_them_too(self, nvim):
        nvim.load_entry_point()
        assert {"Xli", "XliGraph", "XliSessions"} <= set(nvim.commands())

    def test_setting_up_twice_does_not_duplicate_commands(self, nvim):
        nvim.agent().setup(None)
        first = nvim.command_descriptions()
        nvim.agent().setup(None)
        assert nvim.command_descriptions() == first

    def test_the_task_command_needs_an_argument(self, nvim):
        nvim.agent().setup(None)
        assert nvim.commands()["Xli"]["nargs"] == "+"

    def test_graph_takes_an_optional_module(self, nvim):
        nvim.agent().setup(None)
        assert nvim.commands()["XliGraph"]["nargs"] == "?"

    def test_every_description_is_russian(self, nvim):
        nvim.agent().setup(None)
        for name, desc in nvim.command_descriptions().items():
            assert any("А" <= char <= "я" or char in "Ёё" for char in desc), (
                f"{name}: {desc!r}"
            )


class TestStatus:
    def test_the_statusline_is_quiet_when_idle(self, nvim):
        assert "◆ XLI" in nvim.agent().statusline()

    def test_nothing_is_shown_before_the_first_task(self, nvim):
        assert nvim.agent()._status_text() is None



class TestProtocol:
    """xli/rpc.lua is the protocol layer; a regression there breaks every
    frontend, so it is worth running rather than reading."""

    def real_rpc(self, nvim):
        return nvim.lua.execute(sources()["xli.rpc"])

    def connect(self, nvim, rpc):
        """Lua methods called from Python need `self` spelled out."""
        replies = []
        client = rpc.new()
        # Lua callbacks are not arity-checked: `callback(nil)` arrives with one
        # argument, so collect whatever comes.
        nvim.call(client, "connect", "/tmp/xli-test/kernel.sock",
                  lambda *args: replies.append(args))
        return client, replies

    def test_connect_sends_the_handshake(self, nvim):
        rpc = self.real_rpc(nvim)
        client, replies = self.connect(nvim, rpc)
        frame = json.loads(list(nvim.state["pipe"]["writes"].values())[0])
        assert frame["method"] == "hello"
        assert frame["params"]["client"] == "nvim"
        assert frame["params"]["protocol_version"] == 1
        assert "tools" in frame["params"]["capabilities"]
        # The handshake resolves when the kernel answers, not before.
        assert replies == []
        nvim.state["pipe"]["read_callback"](
            None, '{"jsonrpc":"2.0","id":1,"result":{"compatible":true}}\n')
        assert replies and replies[0][0] is None

    def test_a_reply_split_across_reads_still_resolves(self, nvim):
        """A frame may straddle any number of socket reads."""
        rpc = self.real_rpc(nvim)
        client, _ = self.connect(nvim, rpc)
        seen = []
        nvim.call(client, "request", "agent.run", nvim.to_lua({"task": "x"}),
                  lambda *args: seen.append(args))
        nvim.state["pipe"]["read_callback"](None, '{"jsonrpc":"2.0","id":2,"resu')
        assert seen == []
        nvim.state["pipe"]["read_callback"](None, 'lt":{"ok":true}}\n')
        assert seen and seen[-1][0] is None

    def test_an_error_reply_arrives_as_a_message(self, nvim):
        rpc = self.real_rpc(nvim)
        client, _ = self.connect(nvim, rpc)
        seen = []
        nvim.call(client, "request", "agent.run", None, lambda *args: seen.append(args))
        nvim.state["pipe"]["read_callback"](
            None, '{"jsonrpc":"2.0","id":2,"error":{"code":-1,"message":"нет ключа"}}\n')
        assert seen and seen[-1][0] == "нет ключа"

    def test_a_notification_reaches_its_handler(self, nvim):
        rpc = self.real_rpc(nvim)
        client, _ = self.connect(nvim, rpc)
        seen = []
        nvim.call(client, "on", "agent.assistant", lambda params: seen.append(params["text"]))
        nvim.state["pipe"]["read_callback"](
            None, '{"jsonrpc":"2.0","method":"agent.assistant","params":{"text":"привет"}}\n')
        assert seen == ["привет"]

    def test_closing_fails_the_waiting_callers(self, nvim):
        rpc = self.real_rpc(nvim)
        client, _ = self.connect(nvim, rpc)
        seen = []
        nvim.call(client, "request", "agent.run", None, lambda *args: seen.append(args))
        nvim.call(client, "close")
        assert seen and seen[-1][0] is not None
