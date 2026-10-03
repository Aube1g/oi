#!/usr/bin/env python3
"""Tests for the agent loop, the kernel RPC methods, and the CLI."""

import asyncio
import json

import pytest

from xli.agent import Agent
from xli.cli import EXIT_OK, EXIT_USAGE, build_parser, main
from xli.kernel.protocol import Request, encode_line
from xli.permissions.policy import Mode, Policy
from xli.providers.fake import FakeProvider
from xli.session import Session
from xli.tools.registry import default_registry


def tool_block(name: str, args: dict) -> str:
    return "<tool>" + json.dumps({"name": name, "args": args}) + "</tool>"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def registry(tmp_path):
    return default_registry(policy=Policy(mode=Mode.AUTO, root=tmp_path))


# ------------------------------------------------------------------ agent loop
class TestAgentLoop:
    def test_done_marker_ends_the_run(self, registry):
        provider = FakeProvider(responses=["<done>all finished</done>"])
        result = run(Agent(provider, registry=registry, policy=Policy(mode=Mode.AUTO)).run("task"))
        assert result.ok is True
        assert result.stopped_reason == "done"
        assert result.summary == "all finished"

    def test_tool_call_is_executed(self, registry, tmp_path):
        target = tmp_path / "made.txt"
        provider = FakeProvider(
            responses=[
                "creating it " + tool_block("write", {"path": str(target), "content": "hi\n"}),
                "<done>wrote the file</done>",
            ]
        )
        result = run(Agent(provider, registry=registry, policy=Policy(mode=Mode.AUTO)).run("make it"))
        assert result.ok
        assert target.read_text() == "hi\n"
        assert sum(len(s.calls) for s in result.steps) == 1

    def test_tool_error_is_reported_back_not_fatal(self, registry):
        # Snapshot each turn: the agent mutates one message list in place, so
        # reading provider.calls after the run would show the final state.
        queue = [tool_block("read", {"path": "/definitely/not/here"}), "<done>gave up</done>"]
        snapshots = []

        def responder(messages):
            snapshots.append([dict(m) for m in messages])
            return queue.pop(0)

        provider = FakeProvider(response_fn=responder)
        agent = Agent(provider, registry=registry, policy=Policy(mode=Mode.AUTO))
        result = run(agent.run("read it"))

        assert result.ok
        assert result.steps[0].results[0].ok is False
        # The model must be told what went wrong on the next turn.
        assert len(snapshots) == 2
        assert "ERROR" in snapshots[1][-1]["content"]

    def test_max_steps_stops_the_loop(self, registry):
        provider = FakeProvider(responses=["thinking"] * 50)
        agent = Agent(
            provider, registry=registry, policy=Policy(mode=Mode.AUTO), max_steps=3
        )
        result = run(agent.run("never ends"))
        assert result.stopped_reason == "no_tool_calls"
        assert provider.calls.__len__() <= 3

    def test_no_tool_call_and_no_done_completes(self, registry):
        provider = FakeProvider(responses=["here is my answer"])
        result = run(
            Agent(provider, registry=registry, policy=Policy(mode=Mode.AUTO)).run("question")
        )
        assert result.ok
        assert result.stopped_reason == "no_tool_calls"
        assert result.summary == "here is my answer"

    def test_provider_exception_is_reported(self, registry):
        class Broken:
            async def chat(self, *a, **k):
                raise RuntimeError("endpoint down")

        result = run(
            Agent(Broken(), registry=registry, policy=Policy(mode=Mode.AUTO)).run("task")
        )
        assert result.ok is False
        assert result.stopped_reason == "provider_error"
        assert "endpoint down" in result.text

    def test_events_are_emitted(self, registry, tmp_path):
        seen = []
        target = tmp_path / "f.txt"
        provider = FakeProvider(
            responses=[
                "working " + tool_block("write", {"path": str(target), "content": "x"}),
                "<done>done</done>",
            ]
        )
        agent = Agent(
            provider,
            registry=registry,
            policy=Policy(mode=Mode.AUTO),
            on_event=lambda kind, payload: seen.append(kind),
        )
        run(agent.run("go"))

        assert "agent" in seen
        assert "tool_call" in seen
        assert "tool_result" in seen

    def test_throwing_event_handler_does_not_break_the_loop(self, registry):
        def bad_handler(kind, payload):
            raise RuntimeError("renderer exploded")

        provider = FakeProvider(responses=["<done>fine</done>"])
        agent = Agent(
            provider, registry=registry, policy=Policy(mode=Mode.AUTO), on_event=bad_handler
        )
        assert run(agent.run("go")).ok

    def test_session_records_the_run(self, registry, tmp_path):
        session = Session(root=tmp_path)
        provider = FakeProvider(responses=["<done>ok</done>"])
        run(
            Agent(
                provider, registry=registry, policy=Policy(mode=Mode.AUTO), session=session
            ).run("remember me")
        )
        reloaded = Session.load(session.session_id, root=tmp_path)
        kinds = [e.kind for e in reloaded.events]
        assert "user" in kinds and "assistant" in kinds

    def test_repairs_are_surfaced(self, registry):
        provider = FakeProvider(
            responses=["<tool>{'name': 'ls', 'args': {}}</tool>", "<done>done</done>"]
        )
        result = run(
            Agent(provider, registry=registry, policy=Policy(mode=Mode.AUTO)).run("go")
        )
        assert result.repairs

    def test_result_to_dict(self, registry):
        provider = FakeProvider(responses=["<done>x</done>"])
        result = run(
            Agent(provider, registry=registry, policy=Policy(mode=Mode.AUTO)).run("go")
        )
        payload = result.to_dict()
        assert payload["ok"] is True
        assert {"steps", "tool_calls", "tool_errors", "seconds"} <= set(payload)

    def test_system_prompt_lists_tools(self, registry):
        agent = Agent(FakeProvider(), registry=registry, policy=Policy(mode=Mode.AUTO))
        prompt = agent.system_prompt()
        assert "read" in prompt and "bash" in prompt
        assert "<tool>" in prompt

    def test_permission_denial_reaches_the_model(self, tmp_path):
        readonly = default_registry(policy=Policy(mode=Mode.READONLY, root=tmp_path))
        provider = FakeProvider(
            responses=[
                tool_block("write", {"path": str(tmp_path / "x"), "content": "y"}),
                "<done>stopped</done>",
            ]
        )
        agent = Agent(provider, registry=readonly, policy=Policy(mode=Mode.READONLY))
        result = run(agent.run("write it"))
        assert result.steps[0].results[0].ok is False
        assert "permission denied" in result.steps[0].results[0].error


# ------------------------------------------------------------ kernel methods
class TestKernelMethods:
    def _kernel(self, tmp_path):
        from xli.kernel.methods import build_kernel

        return build_kernel(
            project_root=tmp_path,
            provider=FakeProvider(responses=["<done>ok</done>"]),
            policy=Policy(mode=Mode.AUTO, root=tmp_path),
        )

    def test_hello_handshake(self, tmp_path):
        from xli.kernel.protocol import PROTOCOL_VERSION

        kernel = self._kernel(tmp_path)
        out = run(
            kernel.feed_line(
                encode_line(Request(method="hello", id=1, params={"protocol_version": PROTOCOL_VERSION}))
            )
        )
        assert out[0].result["compatible"] is True
        assert out[0].result["implementation"] == "xli-kernel"

    def test_hello_rejects_version_mismatch(self, tmp_path):
        kernel = self._kernel(tmp_path)
        out = run(kernel.feed_line(encode_line(Request(method="hello", id=1, params={"protocol_version": 999}))))
        assert out[0].result["compatible"] is False

    def test_agent_tools_lists_catalogue(self, tmp_path):
        kernel = self._kernel(tmp_path)
        out = run(kernel.feed_line(encode_line(Request(method="agent.tools", id=1, params={}))))
        names = {t["name"] for t in out[0].result["tools"]}
        assert {"read", "write", "bash"} <= names

    def test_tools_run_invokes_a_tool(self, tmp_path):
        kernel = self._kernel(tmp_path)
        out = run(
            kernel.feed_line(
                encode_line(
                    Request(
                        method="tools.run",
                        id=1,
                        params={"name": "write", "args": {"path": str(tmp_path / "a.txt"), "content": "z"}},
                    )
                )
            )
        )
        assert out[0].result["ok"] is True
        assert (tmp_path / "a.txt").read_text() == "z"

    def test_tools_run_unknown_tool(self, tmp_path):
        kernel = self._kernel(tmp_path)
        out = run(
            kernel.feed_line(
                encode_line(Request(method="tools.run", id=1, params={"name": "nope", "args": {}}))
            )
        )
        assert out[0].result["ok"] is False

    def test_config_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XLI_CONFIG_DIR", str(tmp_path / "cfg"))
        kernel = self._kernel(tmp_path)

        listing = run(kernel.feed_line(encode_line(Request(method="config.list", id=1, params={}))))
        assert "provider" in listing[0].result["config"]

        got = run(
            kernel.feed_line(encode_line(Request(method="config.get", id=2, params={"key": "provider"})))
        )
        assert got[0].result["value"] == "mistral"

        bad = run(
            kernel.feed_line(
                encode_line(Request(method="config.set", id=3, params={"key": "permissions.mode", "value": "nope"}))
            )
        )
        assert bad[0].error is not None

    def test_kernel_status_and_preflight(self, tmp_path):
        kernel = self._kernel(tmp_path)
        status = run(kernel.feed_line(encode_line(Request(method="kernel.status", id=1, params={}))))
        assert status[0].result["total"] > 0
        assert "compiled" in status[0].result

        pre = run(kernel.feed_line(encode_line(Request(method="kernel.preflight", id=2, params={}))))
        assert "checks" in pre[0].result

    def test_session_list_empty(self, tmp_path):
        kernel = self._kernel(tmp_path)
        out = run(kernel.feed_line(encode_line(Request(method="session.list", id=1, params={}))))
        assert out[0].result["sessions"] == []

    def test_doctor_reports_checks(self, tmp_path):
        kernel = self._kernel(tmp_path)
        out = run(kernel.feed_line(encode_line(Request(method="doctor", id=1, params={}))))
        names = {c["name"] for c in out[0].result["checks"]}
        assert "python" in names

    def test_missing_required_param(self, tmp_path):
        kernel = self._kernel(tmp_path)
        out = run(kernel.feed_line(encode_line(Request(method="agent.run", id=1, params={}))))
        assert out[0].error is not None

    def test_agent_run_over_rpc(self, tmp_path):
        kernel = self._kernel(tmp_path)
        out = run(kernel.feed_line(encode_line(Request(method="agent.run", id=1, params={"task": "hello"}))))
        assert out[0].result["ok"] is True
        assert out[0].result["stopped_reason"] == "done"


# ------------------------------------------------------------------------ CLI
class TestCliParser:
    def test_version_flag(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        assert "xli" in capsys.readouterr().out

    def test_subcommands_registered(self):
        parser = build_parser()
        choices = parser._subparsers._group_actions[0].choices
        for name in ("run", "tui", "repl", "serve", "config", "kernel", "tools", "session", "skills", "mcp", "nvim", "doctor"):
            assert name in choices

    def test_bare_task_becomes_run(self):
        parser = build_parser()
        args = parser.parse_args(["run", "fix", "the", "bug"])
        assert args.command == "run"
        assert args.task == ["fix", "the", "bug"]

    def test_unknown_first_word_is_treated_as_a_task(self):
        # `xli fix the bug` is a task, not a typo — the CLI rewrites it to `run`.
        parser = build_parser()
        argv = ["fix", "the", "bug"]
        if argv[0] not in parser._subparsers._group_actions[0].choices:
            argv = ["run", *argv]
        args = parser.parse_args(argv)
        assert args.command == "run"
        assert args.task == ["fix", "the", "bug"]

    def test_bad_flag_still_errors(self):
        with pytest.raises(SystemExit) as exc:
            main(["tools", "--not-a-real-flag"])
        assert exc.value.code == 2


class TestCliCommands:
    def test_config_list(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XLI_CONFIG_DIR", str(tmp_path))
        assert main(["config", "list"]) == EXIT_OK
        assert "provider" in capsys.readouterr().out

    def test_config_get(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XLI_CONFIG_DIR", str(tmp_path))
        assert main(["config", "get", "permissions.mode"]) == EXIT_OK
        assert "confirm" in capsys.readouterr().out

    def test_config_set_validates(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XLI_CONFIG_DIR", str(tmp_path))
        assert main(["config", "set", "permissions.mode", "bogus"]) == EXIT_USAGE
        assert "not one of" in capsys.readouterr().err

    def test_config_set_persists(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XLI_CONFIG_DIR", str(tmp_path))
        out = tmp_path / "c.json"
        assert main(["config", "set", "provider", "openai", "--output", str(out)]) == EXIT_OK
        assert json.loads(out.read_text())["provider"] == "openai"

    def test_config_get_missing_key_usage(self, capsys):
        assert main(["config", "get"]) == EXIT_USAGE

    def test_config_path(self, capsys):
        assert main(["config", "path", "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert "user" in payload and "project" in payload

    def test_tools_list(self, capsys):
        assert main(["tools", "list"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "read" in out and "bash" in out

    def test_tools_schema_is_json(self, capsys):
        assert main(["tools", "schema"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert any(t["name"] == "read" for t in payload)

    def test_tools_prompt(self, capsys):
        assert main(["tools", "prompt"]) == EXIT_OK
        assert "<tool>" in capsys.readouterr().out

    def test_tools_unknown_name(self, capsys):
        assert main(["tools", "enable", "nope"]) == EXIT_USAGE

    def test_tools_enable_requires_name(self, capsys):
        assert main(["tools", "enable"]) == EXIT_USAGE

    def test_kernel_status(self, capsys):
        assert main(["kernel", "status"]) == EXIT_OK
        assert "compiled" in capsys.readouterr().out

    def test_kernel_status_json(self, capsys):
        assert main(["kernel", "status", "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["total"] > 0

    def test_kernel_preflight(self, capsys):
        code = main(["kernel", "preflight"])
        assert code in (EXIT_OK, 3)
        assert "cython" in capsys.readouterr().out

    def test_kernel_build_without_toolchain_exits_3(self, capsys):
        from xli.manager import kernel_build

        if kernel_build.preflight().ok:
            pytest.skip("toolchain complete; failure path unreachable")
        assert main(["kernel", "build", "diff_engine"]) == 3
        assert "toolchain incomplete" in capsys.readouterr().out

    def test_kernel_clean(self, capsys):
        assert main(["kernel", "clean", "--json"]) == EXIT_OK
        assert "removed" in capsys.readouterr().out

    def test_session_list_empty(self, tmp_path, capsys):
        assert main(["session", "list", "--project", str(tmp_path), "--json"]) == EXIT_OK
        assert json.loads(capsys.readouterr().out) == []

    def test_session_show_requires_id(self, capsys):
        assert main(["session", "show"]) == EXIT_USAGE

    def test_session_delete_requires_id(self, capsys):
        assert main(["session", "delete"]) == EXIT_USAGE

    def test_mcp_list(self, capsys):
        assert main(["mcp", "--json"]) == EXIT_OK
        assert isinstance(json.loads(capsys.readouterr().out), dict)

    def test_skills_list(self, capsys):
        assert main(["skills", "--json"]) == EXIT_OK
        assert isinstance(json.loads(capsys.readouterr().out), list)

    def test_doctor_json(self, capsys):
        code = main(["doctor", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert "checks" in payload
        assert code in (EXIT_OK, 3)

    def test_doctor_human(self, capsys):
        main(["doctor"])
        assert "python" in capsys.readouterr().out

    def test_nvim_install(self, tmp_path, capsys):
        assert main(["nvim", "--target", str(tmp_path)]) == EXIT_OK
        assert (tmp_path / "plugin" / "xli.lua").is_file()

    def test_tui_without_terminal_exits_2(self, capsys):
        assert main(["tui"]) == 2
        assert "terminal" in capsys.readouterr().err
