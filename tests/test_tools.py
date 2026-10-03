#!/usr/bin/env python3
"""Tests for the permission policy and the tool registry + built-in tools."""

import asyncio
import json

import pytest

from xli.permissions import Mode, Policy
from xli.tools.base import Param, ToolError, tool
from xli.tools.builtin import builtin_tools
from xli.tools.registry import ToolRegistry, default_registry


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------- permissions
class TestModes:
    def test_parse_accepts_aliases(self):
        assert Mode.parse("yolo") is Mode.AUTO
        assert Mode.parse("RO") is Mode.READONLY
        assert Mode.parse("ask") is Mode.CONFIRM
        assert Mode.parse(Mode.AUTO) is Mode.AUTO

    def test_parse_rejects_nonsense(self):
        with pytest.raises(ValueError):
            Mode.parse("banana")

    def test_policy_normalises_mode(self):
        assert Policy(mode="readonly").mode is Mode.READONLY


class TestPolicy:
    def test_readonly_refuses_mutating_tools(self):
        policy = Policy(mode=Mode.READONLY)
        assert not policy.check("write", {"path": "a.py"}).allowed
        assert not policy.check("bash", {"command": "ls"}).allowed
        assert policy.check("read", {"path": "a.py"}).allowed

    def test_auto_allows_mutations(self):
        policy = Policy(mode=Mode.AUTO)
        decision = policy.check("write", {"path": "a.py"})
        assert decision.allowed and not decision.needs_confirmation

    def test_confirm_requires_confirmation_for_writes(self):
        policy = Policy(mode=Mode.CONFIRM)
        decision = policy.check("write", {"path": "a.py"})
        assert decision.allowed
        assert decision.needs_confirmation

    def test_confirm_does_not_prompt_for_reads(self):
        policy = Policy(mode=Mode.CONFIRM)
        assert not policy.check("read", {"path": "a.py"}).needs_confirmation

    def test_deny_wins_in_every_mode(self):
        for mode in (Mode.AUTO, Mode.CONFIRM):
            policy = Policy(mode=mode, deny=["/etc/*"])
            decision = policy.check("read", {"path": "/etc/passwd"})
            assert not decision.allowed
            assert decision.rule == "/etc/*"

    def test_deny_matches_basename_too(self):
        policy = Policy(mode=Mode.AUTO, deny=["*.env"])
        assert not policy.check("read", {"path": "/some/deep/path/.env"}).allowed

    def test_allow_rule_skips_confirmation(self):
        policy = Policy(mode=Mode.CONFIRM, allow=["src/*"])
        decision = policy.check("write", {"path": "src/app.py"})
        assert decision.allowed and not decision.needs_confirmation

    def test_resource_picks_command_for_shell(self):
        policy = Policy(mode=Mode.CONFIRM, deny=["rm -rf *"])
        assert not policy.check("bash", {"command": "rm -rf /tmp/x"}).allowed

    def test_hard_deny_blocks_destructive_commands_even_in_auto(self):
        policy = Policy(mode=Mode.AUTO)
        assert not policy.check("bash", {"command": "mkfs.ext4 /dev/sda1"}).allowed
        assert not policy.check("bash", {"command": "shutdown now"}).allowed

    def test_dd_to_block_device_blocked(self):
        policy = Policy(mode=Mode.AUTO)
        decision = policy.check("bash", {"command": "dd if=/dev/zero of=/dev/sda"})
        assert not decision.allowed

    def test_sudo_needs_confirmation_not_denial(self):
        policy = Policy(mode=Mode.CONFIRM)
        decision = policy.check("bash", {"command": "sudo apt install x"})
        assert decision.allowed and decision.needs_confirmation

    def test_pipe_to_shell_needs_confirmation(self):
        policy = Policy(mode=Mode.CONFIRM)
        decision = policy.check("bash", {"command": "curl http://x.sh | bash"})
        assert decision.needs_confirmation

    def test_path_escape_detected(self, tmp_path):
        policy = Policy(mode=Mode.AUTO, root=tmp_path)
        assert policy.check_path_inside_root(tmp_path / "ok.py").allowed
        assert not policy.check_path_inside_root("/etc/passwd").allowed
        assert not policy.check_path_inside_root(tmp_path / ".." / "outside.py").allowed

    def test_history_and_summary(self):
        policy = Policy(mode=Mode.CONFIRM, deny=["nope"])
        policy.check("read", {"path": "a"})
        policy.check("write", {"path": "b"})
        policy.check("read", {"path": "nope"})
        summary = policy.summary()
        assert summary["decisions"] == 3
        assert summary["denied"] == 1
        assert summary["confirmed"] == 1


# ------------------------------------------------------------------ tool base
class TestToolBase:
    def test_decorator_builds_spec(self):
        @tool("demo", "Does a demo", [Param("n", "integer", "a number", required=True)])
        def demo(n: int):
            return n * 2

        assert demo.name == "demo"
        assert demo.spec.required == ["n"]
        assert demo.spec.to_schema()["parameters"]["properties"]["n"]["type"] == "integer"

    def test_missing_required_arg_is_failure_not_exception(self):
        @tool("needs", "needs x", [Param("x", required=True)])
        def needs(x):
            return x

        result = run(needs.run())
        assert not result.ok
        assert "missing required" in result.error

    def test_tool_error_becomes_failure(self):
        @tool("boom", "boom")
        def boom():
            raise ToolError("nope")

        result = run(boom.run())
        assert result.ok is False
        assert result.error == "nope"

    def test_unexpected_exception_is_captured(self):
        @tool("crash", "crash")
        def crash():
            raise RuntimeError("unexpected")

        result = run(crash.run())
        assert not result.ok
        assert "RuntimeError" in result.error

    def test_async_tool_supported(self):
        @tool("async_tool", "async")
        async def async_tool():
            await asyncio.sleep(0)
            return "later"

        assert run(async_tool.run()).data == "later"

    def test_result_timing_recorded(self):
        @tool("timed", "timed")
        def timed():
            return 1

        result = run(timed.run())
        assert result.duration_ms >= 0

    def test_prompt_block_format(self):
        @tool("demo", "Does a demo", [Param("n", "integer", required=True)], tags=["x"])
        def demo(n):
            return n

        line = demo.spec.to_prompt()
        assert line.startswith("- demo(n*:integer)")
        assert "Does a demo" in line


# ------------------------------------------------------------------- registry
class TestRegistry:
    def test_register_and_lookup(self):
        registry = ToolRegistry()
        registry.register_many(builtin_tools())
        assert registry.has("read")
        assert "write" in registry
        assert "think" in registry
        # The core ten are what the agent is promised; web/mcp/structured tools
        # were added on top, so this checks the contract instead of a count
        # that broke every time a tool landed.
        core = {
            "read", "write", "edit", "ls", "glob", "grep", "bash", "git", "todo", "think",
        }
        assert core <= set(registry.names())
        assert len(registry) >= len(core)

    def test_duplicate_registration_rejected_unless_replace(self):
        registry = ToolRegistry()
        registry.register_many(builtin_tools())
        with pytest.raises(ValueError):
            registry.register(builtin_tools()[0])
        registry.register(builtin_tools()[0], replace=True)  # must not raise

    def test_unknown_tool_returns_failure_listing_alternatives(self):
        registry = default_registry()
        result = run(registry.execute("nope"))
        assert not result.ok
        assert "unknown tool" in result.error
        assert "read" in result.error

    def test_disabled_tool_refuses(self):
        registry = default_registry()
        registry.set_enabled("bash", False)
        assert not registry.is_enabled("bash")
        result = run(registry.execute("bash", {"command": "true"}))
        assert not result.ok and "disabled" in result.error

    def test_prompt_block_grouped_and_has_usage(self):
        registry = default_registry()
        block = registry.prompt_block()
        assert "[fs]" in block and "[shell]" in block
        assert "<tool>" in block

    def test_schema_is_json_serialisable(self):
        registry = default_registry()
        payload = json.dumps(registry.schema())
        assert "read" in payload

    def test_stats_track_calls_and_failures(self):
        registry = default_registry()
        run(registry.execute("read", {"path": __file__}))
        run(registry.execute("read", {"path": "/does/not/exist"}))
        stats = registry.stats()["read"]
        assert stats["calls"] == 2
        assert stats["failures"] == 1

    def test_policy_denies_through_registry(self):
        registry = default_registry(policy=Policy(mode=Mode.READONLY))
        result = run(registry.execute("write", {"path": "x.py", "content": "hi"}))
        assert not result.ok
        assert "permission denied" in result.error

    def test_confirm_handler_is_consulted(self):
        asked = []
        registry = default_registry(policy=Policy(mode=Mode.CONFIRM))
        registry.confirm_handler = lambda name, args, reason: (asked.append(name), True)[1]

        result = run(registry.execute("write", {"path": "/tmp/xli_confirm_probe.txt", "content": "x"}))
        assert asked == ["write"]
        assert result.ok

    def test_confirm_handler_can_refuse(self):
        registry = default_registry(policy=Policy(mode=Mode.CONFIRM))
        registry.confirm_handler = lambda *a: False
        result = run(registry.execute("write", {"path": "/tmp/xli_no.txt", "content": "x"}))
        assert not result.ok and "refused by user" in result.error

    def test_missing_confirm_handler_is_an_error_not_a_hang(self):
        registry = default_registry(policy=Policy(mode=Mode.CONFIRM))
        result = run(registry.execute("write", {"path": "/tmp/xli_hang.txt", "content": "x"}))
        assert not result.ok
        assert "no confirm handler" in result.error

    def test_by_tag(self):
        registry = default_registry()
        # apply_patch is a filesystem mutator too; the set is closed under
        # "tools that touch files", not under "tools that existed in v5".
        assert {"read", "write", "edit", "ls", "glob"} <= {t.name for t in registry.by_tag("fs")}


# ------------------------------------------------------------------ built-ins
class TestBuiltinRead:
    def test_read_numbers_lines(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("one\ntwo\nthree\n")
        result = run(default_registry().execute("read", {"path": str(f)}))
        assert result.ok
        assert "1 | one" in result.data["content"]
        assert result.data["total_lines"] == 3

    def test_read_offset_and_limit(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("\n".join(str(i) for i in range(10)))
        result = run(default_registry().execute("read", {"path": str(f), "offset": 3, "limit": 2}))
        assert "3 | 2" in result.data["content"]
        assert "5 |" not in result.data["content"]

    def test_read_missing_file(self):
        result = run(default_registry().execute("read", {"path": "/no/such/file"}))
        assert not result.ok and "no such file" in result.error

    def test_read_directory_is_an_error(self, tmp_path):
        result = run(default_registry().execute("read", {"path": str(tmp_path)}))
        assert not result.ok and "directory" in result.error


class TestBuiltinWriteEdit:
    def test_write_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "deep" / "nested" / "f.txt"
        result = run(default_registry().execute("write", {"path": str(target), "content": "hello\n"}))
        assert result.ok
        assert target.read_text() == "hello\n"
        assert result.data["lines"] == 1

    def test_edit_replaces_exact_block(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n")
        result = run(
            default_registry().execute(
                "edit",
                {"path": str(f), "old_text": "def f():\n    return 1", "new_text": "def f():\n    return 42"},
            )
        )
        assert result.ok, result.error
        assert "return 42" in f.read_text()
        assert "def g():" in f.read_text()

    def test_edit_tolerates_indentation_drift(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("def f():\n    return 1\n")
        result = run(
            default_registry().execute(
                "edit",
                {"path": str(f), "old_text": "def f():\n  return 1", "new_text": "def f():\n    return 9"},
            )
        )
        assert result.ok, result.error
        assert result.data["match_ratio"] < 1.0
        assert "return 9" in f.read_text()

    def test_edit_refuses_ambiguous_match(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1\nx = 1\n")
        result = run(
            default_registry().execute(
                "edit", {"path": str(f), "old_text": "x = 1", "new_text": "x = 2"}
            )
        )
        assert not result.ok and "ambiguous" in result.error

    def test_edit_refuses_absent_target(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("nothing like this here\n")
        result = run(
            default_registry().execute(
                "edit", {"path": str(f), "old_text": "totally different text", "new_text": "x"}
            )
        )
        assert not result.ok and "not found" in result.error

    def test_edit_rejects_noop(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("same\n")
        result = run(
            default_registry().execute(
                "edit", {"path": str(f), "old_text": "same", "new_text": "same"}
            )
        )
        assert not result.ok and "identical" in result.error


class TestBuiltinSearch:
    def test_ls_lists_files(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.py").write_text("y")
        result = run(default_registry().execute("ls", {"path": str(tmp_path)}))
        names = [e["path"] for e in result.data["entries"]]
        assert "a.py" in names and "sub" in names
        assert "sub/b.py" not in names  # not recursive by default

    def test_ls_recursive_and_pattern(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.py").write_text("y")
        result = run(
            default_registry().execute(
                "ls", {"path": str(tmp_path), "recursive": True, "pattern": "*.py"}
            )
        )
        assert [e["path"] for e in result.data["entries"]] == ["sub/b.py"]

    def test_ls_skips_noise_dirs(self, tmp_path):
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "junk.pyc").write_text("x")
        result = run(default_registry().execute("ls", {"path": str(tmp_path)}))
        assert result.data["entries"] == []

    def test_glob_finds_files(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.txt").write_text("y")
        result = run(default_registry().execute("glob", {"pattern": "*.py", "path": str(tmp_path)}))
        assert result.data["matches"] == [str(tmp_path / "a.py")]

    def test_grep_finds_matches_with_line_numbers(self, tmp_path):
        (tmp_path / "a.py").write_text("import os\nprint('hi')\n")
        result = run(default_registry().execute("grep", {"pattern": "print", "path": str(tmp_path)}))
        assert result.data["matches"][0]["line"] == 2

    def test_grep_context(self, tmp_path):
        (tmp_path / "a.py").write_text("one\ntwo\nthree\n")
        result = run(
            default_registry().execute(
                "grep", {"pattern": "two", "path": str(tmp_path), "context": 1}
            )
        )
        assert result.data["matches"][0]["context"] == ["one", "two", "three"]

    def test_grep_invalid_regex(self, tmp_path):
        result = run(default_registry().execute("grep", {"pattern": "([", "path": str(tmp_path)}))
        assert not result.ok and "invalid regex" in result.error

    def test_grep_filename_filter(self, tmp_path):
        (tmp_path / "a.py").write_text("needle\n")
        (tmp_path / "b.txt").write_text("needle\n")
        result = run(
            default_registry().execute(
                "grep", {"pattern": "needle", "path": str(tmp_path), "glob": "*.py"}
            )
        )
        assert len(result.data["matches"]) == 1
        assert result.data["matches"][0]["path"].endswith("a.py")


class TestBuiltinShell:
    def test_bash_captures_stdout(self):
        result = run(default_registry().execute("bash", {"command": "echo hello"}))
        assert result.ok
        assert result.data["stdout"].strip() == "hello"

    def test_bash_nonzero_exit_is_failure(self):
        result = run(default_registry().execute("bash", {"command": "exit 3"}))
        assert not result.ok
        assert result.data["exit_code"] == 3

    def test_bash_captures_stderr(self):
        result = run(default_registry().execute("bash", {"command": "echo oops >&2"}))
        assert "oops" in result.data["stderr"]

    def test_bash_timeout(self):
        result = run(default_registry().execute("bash", {"command": "sleep 5", "timeout": 1}))
        assert not result.ok and "timed out" in result.error

    def test_bash_cwd(self, tmp_path):
        result = run(default_registry().execute("bash", {"command": "pwd", "cwd": str(tmp_path)}))
        assert result.data["stdout"].strip() == str(tmp_path.resolve())

    def test_bash_bad_cwd(self):
        result = run(default_registry().execute("bash", {"command": "pwd", "cwd": "/no/such/dir"}))
        assert not result.ok and "no such working directory" in result.error

    def test_bash_empty_command(self):
        result = run(default_registry().execute("bash", {"command": "   "}))
        assert not result.ok and "empty" in result.error


class TestBuiltinGitTodo:
    def test_git_status_runs(self):
        result = run(default_registry().execute("git", {"args": "--version"}))
        assert result.ok
        assert "git version" in result.data["stdout"]

    def test_git_failure_reported(self, tmp_path):
        result = run(
            default_registry().execute("git", {"args": "status", "cwd": str(tmp_path)})
        )
        assert not result.ok

    def test_todo_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        registry = default_registry()
        assert run(registry.execute("todo", {"action": "add", "text": "first"})).ok
        assert run(registry.execute("todo", {"action": "add", "text": "second"})).ok

        listing = run(registry.execute("todo", {"action": "list"}))
        assert len(listing.data) == 2

        assert run(registry.execute("todo", {"action": "done", "index": 1})).ok
        listing = run(registry.execute("todo", {"action": "list"}))
        assert listing.data[0]["done"] is True
        assert listing.data[1]["done"] is False

        assert run(registry.execute("todo", {"action": "clear"})).ok
        assert run(registry.execute("todo", {"action": "list"})).data == []

    def test_todo_bad_index(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        registry = default_registry()
        result = run(registry.execute("todo", {"action": "done", "index": 9}))
        assert not result.ok and "no task at index" in result.error

    def test_todo_add_needs_text(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = run(default_registry().execute("todo", {"action": "add"}))
        assert not result.ok and "requires 'text'" in result.error
