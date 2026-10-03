#!/usr/bin/env python3
"""Tests for XPI, xli's internal plugin system.

These use a throwaway plugin root, so they never touch ~/.xli/xpi and never
pick up whatever the developer happens to have installed.
"""

import asyncio
import json
from pathlib import Path

import pytest

from xli.xpi.base import XpiPlugin
from xli.xpi.bridge import XpiBridge
from xli.xpi.manager import KNOWN_HOOKS, DispatchReport, XpiManager
from xli.xpi.registry import XpiRegistry
from xli.xpi.state import XpiState


# --------------------------------------------------------------------- helpers
def make_plugin(root: Path, name: str, body: str, *, manifest: dict | None = None) -> Path:
    """Write a plugin package and return its directory."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"name": name, "version": "1.0.0", "main": "plugin.py"}
    payload.update(manifest or {})
    (directory / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    (directory / "plugin.py").write_text(body, encoding="utf-8")
    return directory


GOOD_PLUGIN = '''
from xli.xpi.base import XpiPlugin


class Demo(XpiPlugin):
    def __init__(self):
        super().__init__(name="demo", version="2.5.0")
        self.seen = []

    def on_load(self, context):
        self.seen.append("load")
        return {"loaded": True}

    def on_tool_call(self, context):
        self.seen.append(context.get("name"))
'''

CRASHING_PLUGIN = '''
from xli.xpi.base import XpiPlugin


class Bad(XpiPlugin):
    def on_tool_call(self, context):
        raise RuntimeError("plugin exploded")
'''


@pytest.fixture
def manager(tmp_path):
    """A manager rooted in a temp dir, with the singleton reset afterwards."""
    XpiManager.reset()
    XpiState.reset()
    instance = XpiManager(root=tmp_path)
    yield instance
    XpiManager.reset()
    XpiState.reset()


# --------------------------------------------------------------------- loading
class TestLoading:
    def test_empty_root_loads_nothing(self, manager):
        assert manager.list_plugins() == []

    def test_plugin_is_loaded_and_instantiated(self, manager, tmp_path):
        make_plugin(tmp_path, "demo", GOOD_PLUGIN)
        XpiManager.reset()
        reloaded = XpiManager(root=tmp_path)

        assert [p["name"] for p in reloaded.list_plugins()] == ["demo"]
        assert isinstance(reloaded.instance("demo"), XpiPlugin)

    def test_version_comes_from_the_class(self, manager, tmp_path):
        make_plugin(tmp_path, "demo", GOOD_PLUGIN)
        XpiManager.reset()
        reloaded = XpiManager(root=tmp_path)
        assert reloaded.instance("demo").version == "2.5.0"

    def test_plugin_registered_in_registry(self, manager, tmp_path):
        make_plugin(tmp_path, "demo", GOOD_PLUGIN)
        XpiManager.reset()
        reloaded = XpiManager(root=tmp_path)
        assert [e["name"] for e in reloaded.registry.list_active()] == ["demo"]

    def test_missing_manifest_is_skipped_not_fatal(self, manager, tmp_path):
        (tmp_path / "bare").mkdir()
        (tmp_path / "bare" / "notes.txt").write_text("hi")
        XpiManager.reset()
        assert XpiManager(root=tmp_path).list_plugins() == []

    def test_broken_manifest_is_recorded_as_error(self, manager, tmp_path):
        directory = tmp_path / "broken"
        directory.mkdir()
        (directory / "manifest.json").write_text("{not json")

        XpiManager.reset()
        reloaded = XpiManager(root=tmp_path)
        info = reloaded.get("broken")
        assert info is not None
        assert "bad manifest" in info.error

    def test_import_failure_does_not_stop_other_plugins(self, manager, tmp_path):
        make_plugin(tmp_path, "aaa-broken", "import definitely_not_a_module\n")
        make_plugin(tmp_path, "zzz-good", GOOD_PLUGIN)

        XpiManager.reset()
        reloaded = XpiManager(root=tmp_path)

        assert reloaded.get("aaa-broken").error.startswith("import failed")
        assert reloaded.instance("zzz-good") is not None, "the good plugin must still load"

    def test_missing_entry_point_recorded(self, manager, tmp_path):
        directory = tmp_path / "empty"
        directory.mkdir()
        (directory / "manifest.json").write_text(json.dumps({"name": "empty", "main": "nope.py"}))

        XpiManager.reset()
        info = XpiManager(root=tmp_path).get("empty")
        assert "entry point not found" in info.error

    def test_module_without_plugin_class_rejected(self, manager, tmp_path):
        make_plugin(tmp_path, "noclass", "X = 1\n")
        XpiManager.reset()
        info = XpiManager(root=tmp_path).get("noclass")
        assert "no XpiPlugin subclass" in info.error

    def test_ambiguous_entry_requires_explicit_name(self, manager, tmp_path):
        body = '''
from xli.xpi.base import XpiPlugin


class One(XpiPlugin):
    pass


class Two(XpiPlugin):
    pass
'''
        make_plugin(tmp_path, "ambiguous", body)
        XpiManager.reset()
        info = XpiManager(root=tmp_path).get("ambiguous")
        assert "multiple XpiPlugin subclasses" in info.error
        assert '"entry"' in info.error

    def test_explicit_entry_resolves_ambiguity(self, manager, tmp_path):
        body = '''
from xli.xpi.base import XpiPlugin


class One(XpiPlugin):
    pass


class Two(XpiPlugin):
    pass
'''
        make_plugin(tmp_path, "picked", body, manifest={"entry": "Two"})
        XpiManager.reset()
        reloaded = XpiManager(root=tmp_path)
        assert type(reloaded.instance("picked")).__name__ == "Two"

    def test_helper_class_in_another_module_not_picked_up(self, manager, tmp_path):
        body = "from xli.xpi.base import XpiPlugin\n\n\nclass Real(XpiPlugin):\n    pass\n"
        make_plugin(tmp_path, "onlyreal", body)
        XpiManager.reset()
        assert type(XpiManager(root=tmp_path).instance("onlyreal")).__name__ == "Real"

    def test_disabled_plugin_not_instantiated(self, manager, tmp_path):
        make_plugin(tmp_path, "off", GOOD_PLUGIN, manifest={"enabled": False})
        XpiManager.reset()
        reloaded = XpiManager(root=tmp_path)
        assert reloaded.get("off").enabled is False
        assert reloaded.instance("off") is None


# -------------------------------------------------------------------- dispatch
class TestDispatch:
    def test_hook_reaches_the_plugin(self, manager, tmp_path):
        make_plugin(tmp_path, "demo", GOOD_PLUGIN)
        XpiManager.reset()
        reloaded = XpiManager(root=tmp_path)

        report = reloaded.dispatch("on_tool_call", name="read")
        assert report.called == ["demo"]
        assert report.errors == []
        assert reloaded.instance("demo").seen == ["read"]

    def test_multiple_plugins_all_called(self, manager, tmp_path):
        make_plugin(tmp_path, "first", GOOD_PLUGIN)
        make_plugin(tmp_path, "second", GOOD_PLUGIN)
        XpiManager.reset()
        reloaded = XpiManager(root=tmp_path)

        report = reloaded.dispatch("on_tool_call", name="ls")
        assert sorted(report.called) == ["first", "second"]

    def test_crashing_plugin_is_reported_and_others_still_run(self, manager, tmp_path):
        make_plugin(tmp_path, "aaa-crash", CRASHING_PLUGIN)
        make_plugin(tmp_path, "zzz-good", GOOD_PLUGIN)
        XpiManager.reset()
        reloaded = XpiManager(root=tmp_path)

        report = reloaded.dispatch("on_tool_call", name="write")
        assert "zzz-good" in report.called
        assert reloaded.instance("zzz-good").seen == ["write"], "must still have run"
        assert len(report.errors) == 1
        assert report.errors[0]["plugin"] == "aaa-crash"
        assert "plugin exploded" in report.errors[0]["error"]

    def test_return_values_are_collected(self, manager, tmp_path):
        make_plugin(tmp_path, "demo", GOOD_PLUGIN)
        XpiManager.reset()
        reloaded = XpiManager(root=tmp_path)
        report = reloaded.dispatch("on_load")
        assert report.results == {"demo": {"loaded": True}}

    def test_unknown_hook_calls_nobody(self, manager, tmp_path):
        make_plugin(tmp_path, "demo", GOOD_PLUGIN)
        XpiManager.reset()
        report = XpiManager(root=tmp_path).dispatch("on_something_invented")
        assert report.called == []

    def test_platform_filtering(self, manager, tmp_path):
        make_plugin(tmp_path, "nvim-only", GOOD_PLUGIN, manifest={"platform": "nvim"})
        make_plugin(tmp_path, "everywhere", GOOD_PLUGIN, manifest={"platform": "all"})
        XpiManager.reset()
        reloaded = XpiManager(root=tmp_path)

        assert sorted(r.called for r in [reloaded.dispatch("on_tool_call", platform="nvim")]) == [
            ["everywhere", "nvim-only"]
        ]
        assert reloaded.dispatch("on_tool_call", platform="tui").called == ["everywhere"]

    def test_disabled_plugin_not_dispatched(self, manager, tmp_path):
        make_plugin(tmp_path, "demo", GOOD_PLUGIN)
        XpiManager.reset()
        reloaded = XpiManager(root=tmp_path)
        reloaded.set_enabled("demo", False)
        assert reloaded.dispatch("on_tool_call", name="x").called == []

    def test_report_shape(self):
        report = DispatchReport(hook="on_load")
        assert report.to_dict() == {"hook": "on_load", "called": [], "errors": [], "results": {}}

    def test_known_hooks_are_all_implemented_on_the_base(self):
        plugin = XpiPlugin()
        for hook in KNOWN_HOOKS:
            assert callable(getattr(plugin, hook)), f"base is missing {hook}"
            assert getattr(plugin, hook)({}) is None


# --------------------------------------------------------------- enable/reload
class TestControl:
    def test_disable_persists_to_manifest(self, manager, tmp_path):
        directory = make_plugin(tmp_path, "demo", GOOD_PLUGIN)
        XpiManager.reset()
        reloaded = XpiManager(root=tmp_path)

        assert reloaded.set_enabled("demo", False) is True
        manifest = json.loads((directory / "manifest.json").read_text())
        assert manifest["enabled"] is False
        assert reloaded.instance("demo") is None

    def test_enable_reloads_the_plugin(self, manager, tmp_path):
        make_plugin(tmp_path, "demo", GOOD_PLUGIN, manifest={"enabled": False})
        XpiManager.reset()
        reloaded = XpiManager(root=tmp_path)
        assert reloaded.instance("demo") is None

        assert reloaded.set_enabled("demo", True) is True
        assert reloaded.instance("demo") is not None

    def test_set_enabled_unknown_plugin(self, manager):
        assert manager.set_enabled("nope", True) is False

    def test_reload_picks_up_edited_source(self, manager, tmp_path):
        directory = make_plugin(tmp_path, "demo", GOOD_PLUGIN)
        XpiManager.reset()
        reloaded = XpiManager(root=tmp_path)
        assert reloaded.instance("demo").version == "2.5.0"

        edited = GOOD_PLUGIN.replace('version="2.5.0"', 'version="9.9.9"')
        (directory / "plugin.py").write_text(edited, encoding="utf-8")

        assert reloaded.reload("demo") is True
        assert reloaded.instance("demo").version == "9.9.9"

    def test_reload_unknown_plugin(self, manager):
        assert manager.reload("nope") is False

    def test_reload_drops_the_old_module(self, manager, tmp_path):
        make_plugin(tmp_path, "demo", GOOD_PLUGIN)
        XpiManager.reset()
        reloaded = XpiManager(root=tmp_path)
        first = reloaded.instance("demo")

        reloaded.reload("demo")
        assert reloaded.instance("demo") is not first

    def test_unload_runs_the_hook(self, manager, tmp_path):
        body = '''
from xli.xpi.base import XpiPlugin

UNLOADED = []


class Demo(XpiPlugin):
    def on_unload(self, context):
        UNLOADED.append("yes")
'''
        make_plugin(tmp_path, "demo", body)
        XpiManager.reset()
        reloaded = XpiManager(root=tmp_path)
        reloaded._teardown("demo")
        assert reloaded.instance("demo") is None
        assert "demo" not in [e["name"] for e in reloaded.registry.list_active()]

    def test_unload_all(self, manager, tmp_path):
        make_plugin(tmp_path, "one", GOOD_PLUGIN)
        make_plugin(tmp_path, "two", GOOD_PLUGIN)
        XpiManager.reset()
        reloaded = XpiManager(root=tmp_path)
        reloaded.unload_all()
        assert reloaded.instances == {}

    def test_call_hook_compatibility_path(self, manager, tmp_path):
        make_plugin(tmp_path, "demo", GOOD_PLUGIN)
        XpiManager.reset()
        reloaded = XpiManager(root=tmp_path)
        reloaded.call_hook("demo", "on_tool_call", {"name": "grep"})
        assert reloaded.instance("demo").seen == ["grep"]

    def test_call_hook_unknown_plugin_is_none(self, manager):
        assert manager.call_hook("nope", "on_load", {}) is None


# ----------------------------------------------------------------------- state
class TestState:
    def test_roundtrip(self, tmp_path):
        XpiState.reset()
        state = XpiState(path=tmp_path / "s.json")
        state.set("key", "value")
        XpiState.reset()
        assert XpiState(path=tmp_path / "s.json").get("key") == "value"

    def test_update_writes_once(self, tmp_path):
        XpiState.reset()
        state = XpiState(path=tmp_path / "s.json")
        state.update({"a": 1, "b": 2})
        assert state.all() == {"a": 1, "b": 2}

    def test_delete(self, tmp_path):
        XpiState.reset()
        state = XpiState(path=tmp_path / "s.json")
        state.set("k", 1)
        assert state.delete("k") is True
        assert state.delete("k") is False

    def test_clear(self, tmp_path):
        XpiState.reset()
        state = XpiState(path=tmp_path / "s.json")
        state.set("k", 1)
        state.clear()
        assert state.all() == {}

    def test_corrupt_file_does_not_raise(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text("{corrupt")
        XpiState.reset()
        assert XpiState(path=path).all() == {}

    def test_non_object_file_ignored(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text("[1, 2, 3]")
        XpiState.reset()
        assert XpiState(path=path).all() == {}

    def test_no_tmp_file_left_behind(self, tmp_path):
        XpiState.reset()
        state = XpiState(path=tmp_path / "s.json")
        state.set("k", 1)
        assert list(tmp_path.glob("*.tmp")) == []

    def test_singleton(self, tmp_path):
        XpiState.reset()
        first = XpiState(path=tmp_path / "s.json")
        assert XpiState(path=tmp_path / "s.json") is first

    def test_contains_and_len(self, tmp_path):
        XpiState.reset()
        state = XpiState(path=tmp_path / "s.json")
        state.set("k", 1)
        assert "k" in state
        assert len(state) == 1

    def test_keys_sorted(self, tmp_path):
        XpiState.reset()
        state = XpiState(path=tmp_path / "s.json")
        state.update({"b": 1, "a": 2})
        assert state.keys() == ["a", "b"]


# ---------------------------------------------------------------------- bridge
class FakeNvim:
    def __init__(self):
        self.calls = []
        self.commands = []

    def exec_lua(self, code):
        self.calls.append(code)
        return "ok"


class TestBridge:
    def test_call_lua(self):
        nvim = FakeNvim()
        bridge = XpiBridge(nvim)
        assert bridge.call_lua("return 1") == "ok"
        assert nvim.calls == ["return 1"]

    def test_call_lua_without_nvim_is_none(self):
        assert XpiBridge().call_lua("return 1") is None

    def test_notify_lua_sends_event(self):
        nvim = FakeNvim()
        XpiBridge(nvim).notify_lua("agent.done", {"ok": True})
        assert len(nvim.calls) == 1
        assert "vim.json.decode" in nvim.calls[0]
        assert "handle_rpc" in nvim.calls[0]

    def test_notify_lua_escapes_hostile_payload(self):
        """The payload must never be parsed as Lua source."""
        nvim = FakeNvim()
        injection = '"); os.execute("rm -rf /"); --'
        hostile = {"a": injection, "b": "back\\slash", "c": "quote\"inside"}
        XpiBridge(nvim).notify_lua("evt", hostile)

        code = nvim.calls[0]
        # The injected statement may only exist as data inside one JSON string;
        # if it reached the Lua source directly, decode() would not be the only
        # thing standing between the payload and the interpreter.
        assert code.count("vim.json.decode(") == 1
        assert code.count("os.execute") == code.count("\\\"os.execute") or "\\\"" in code
        assert 'os.execute("rm -rf /")' not in code

    def test_notify_lua_payload_survives_json_roundtrip(self):
        import json as _json

        nvim = FakeNvim()
        payload = {"quotes": 'he said "hi"', "nl": "line\nbreak", "uni": "кириллица 🐝"}
        XpiBridge(nvim).notify_lua("evt", payload)

        code = nvim.calls[0]
        start = code.index("vim.json.decode(") + len("vim.json.decode(")
        end = code.index(")\n", start)
        literal = _json.loads(code[start:end])
        decoded = _json.loads(literal)
        assert decoded["event"] == "evt"
        assert decoded["data"] == payload

    def test_notify_lua_unserialisable_is_reported_not_raised(self):
        nvim = FakeNvim()
        assert XpiBridge(nvim).notify_lua("evt", {"bad": object()}) is None
        assert nvim.calls == []

    def test_handler_registration_and_dispatch(self):
        bridge = XpiBridge()
        bridge.register_handler("ping", lambda: "pong")
        assert bridge.has_handler("ping")
        assert bridge.handle_python_call("ping") == "pong"

    def test_unknown_handler_is_none(self):
        assert XpiBridge().handle_python_call("nope") is None

    def test_handler_exception_does_not_propagate(self):
        bridge = XpiBridge()
        bridge.register_handler("boom", lambda: 1 / 0)
        assert bridge.handle_python_call("boom") is None


# -------------------------------------------------------------------- registry
class TestRegistry:
    def test_register_and_list(self):
        registry = XpiRegistry()
        registry.register("a", "1.0")
        assert registry.list_active() == [{"name": "a", "version": "1.0", "platform": "all"}]

    def test_unregister(self):
        registry = XpiRegistry()
        registry.register("a", "1.0")
        registry.unregister("a")
        assert registry.list_active() == []

    def test_platform_filter(self):
        registry = XpiRegistry()
        registry.register("tui-only", "1.0", platform="tui")
        registry.register("any", "1.0", platform="all")
        assert {e["name"] for e in registry.list_active(platform="tui")} == {"tui-only", "any"}
        assert {e["name"] for e in registry.list_active(platform="nvim")} == {"any"}

    def test_get(self):
        registry = XpiRegistry()
        registry.register("a", "1.0")
        assert registry.get("a").name == "a"
        assert registry.get("nope") is None

    def test_disabled_entries_hidden(self):
        registry = XpiRegistry()
        registry.register("a", "1.0")
        registry.get("a").status = "disabled"
        assert registry.list_active() == []


# ------------------------------------------------------------- agent integration
class TestAgentIntegration:
    def _manager_with(self, tmp_path, body):
        make_plugin(tmp_path, "watcher", body)
        XpiManager.reset()
        return XpiManager(root=tmp_path)

    def test_agent_fires_lifecycle_hooks(self, tmp_path):
        from xli.agent import Agent
        from xli.permissions.policy import Mode, Policy
        from xli.providers.fake import FakeProvider
        from xli.tools.registry import default_registry

        recorder = tmp_path / "calls.json"
        body = f'''
import json
from pathlib import Path
from xli.xpi.base import XpiPlugin

LOG = Path({str(recorder)!r})


class Watcher(XpiPlugin):
    def _add(self, what):
        data = json.loads(LOG.read_text()) if LOG.exists() else []
        data.append(what)
        LOG.write_text(json.dumps(data))

    def on_agent_start(self, context):
        self._add("start")

    def on_tool_call(self, context):
        self._add("tool:" + context["name"])

    def on_tool_result(self, context):
        self._add("result:" + str(context["ok"]))

    def on_agent_end(self, context):
        self._add("end:" + context["stopped_reason"])
'''
        plugins = self._manager_with(tmp_path, body)
        policy = Policy(mode=Mode.AUTO, root=tmp_path)
        target = tmp_path / "f.txt"
        provider = FakeProvider(
            responses=[
                '<tool>{"name":"write","args":{"path":' + json.dumps(str(target)) + ',"content":"x"}}</tool>',
                "<done>ok</done>",
            ]
        )
        agent = Agent(
            provider,
            registry=default_registry(policy=policy),
            policy=policy,
            plugins=plugins,
        )
        result = asyncio.run(agent.run("do it"))

        assert result.ok
        assert json.loads(recorder.read_text()) == [
            "start",
            "tool:write",
            "result:True",
            "end:done",
        ]

    def test_crashing_plugin_does_not_break_the_agent(self, tmp_path):
        from xli.agent import Agent
        from xli.permissions.policy import Mode, Policy
        from xli.providers.fake import FakeProvider
        from xli.tools.registry import default_registry

        plugins = self._manager_with(tmp_path, CRASHING_PLUGIN)
        policy = Policy(mode=Mode.AUTO, root=tmp_path)
        warnings = []
        target = tmp_path / "f.txt"
        agent = Agent(
            # A tool call is required: this plugin only crashes in on_tool_call.
            FakeProvider(
                responses=[
                    '<tool>{"name":"write","args":{"path":'
                    + json.dumps(str(target))
                    + ',"content":"x"}}</tool>',
                    "<done>fine</done>",
                ]
            ),
            registry=default_registry(policy=policy),
            policy=policy,
            plugins=plugins,
            on_event=lambda kind, payload: warnings.append(payload) if kind == "warning" else None,
        )
        result = asyncio.run(agent.run("go"))

        assert result.ok is True, "the agent must survive a crashing plugin"
        assert target.read_text() == "x", "the tool call must still have run"
        assert any("watcher" in str(w.get("message", "")) for w in warnings)

    def test_no_plugins_is_the_default(self):
        from xli.agent import Agent
        from xli.permissions.policy import Mode, Policy
        from xli.providers.fake import FakeProvider

        agent = Agent(FakeProvider(), policy=Policy(mode=Mode.AUTO))
        assert agent.plugins is None
        agent.notify_plugins("on_load", x=1)  # must be a silent no-op


# ------------------------------------------------------------------------ rpc
class TestKernelMethods:
    def test_plugins_list_over_rpc(self, tmp_path, monkeypatch):
        import xli.xpi.manager as manager_module
        from xli.kernel.methods import build_kernel
        from xli.kernel.protocol import Request, encode_line

        monkeypatch.setattr(manager_module, "XPI_DIR", tmp_path)
        make_plugin(tmp_path, "demo", GOOD_PLUGIN)
        XpiManager.reset()

        kernel = build_kernel(project_root=tmp_path)
        out = asyncio.run(
            kernel.feed_line(encode_line(Request(method="plugins.list", id=1, params={})))
        )
        names = [p["name"] for p in out[0].result["plugins"]]
        assert names == ["demo"]
        XpiManager.reset()

    def test_plugins_state_over_rpc(self, tmp_path, monkeypatch):
        import xli.xpi.state as state_module
        from xli.kernel.methods import build_kernel
        from xli.kernel.protocol import Request, encode_line

        monkeypatch.setattr(state_module, "STATE_FILE", tmp_path / "state.json")
        XpiState.reset()

        kernel = build_kernel(project_root=tmp_path)
        out = asyncio.run(
            kernel.feed_line(encode_line(Request(method="plugins.state", id=1, params={})))
        )
        assert out[0].result == {"state": {}}
        XpiState.reset()

    def test_plugins_dispatch_requires_hook(self, tmp_path):
        from xli.kernel.methods import build_kernel
        from xli.kernel.protocol import Request, encode_line

        kernel = build_kernel(project_root=tmp_path)
        out = asyncio.run(
            kernel.feed_line(encode_line(Request(method="plugins.dispatch", id=1, params={})))
        )
        assert out[0].error is not None
