#!/usr/bin/env python3
"""Tests for the subsystems that were previously unwired:

  * xli.utils.file   — atomic writes
  * xli.utils.shell  — SafeShell, now a facade over the shared guard
  * xli.core.context_scout — project scan + AGENTS.md
  * xli.core.inbox   — TeamInbox inter-agent messaging
  * xli.core.self_healing — error classification and retry
"""

import asyncio
import json
import os

import pytest

from xli.core.context_scout import ContextScout
from xli.core.inbox import InboxMessage, TeamInbox
from xli.core.self_healing import (
    SelfHealingEngine,
    get_healing_engine,
    reset_healing_engine,
)
from xli.utils.file import FileOps
from xli.utils.shell import SafeShell


# --------------------------------------------------------------- utils/shell
class TestSafeShell:
    def test_delegates_to_the_shared_blocklist(self):
        """SafeShell must not carry its own copy of the rules."""
        import inspect

        import xli.utils.shell as shell_module

        source = inspect.getsource(shell_module)
        assert "DANGEROUS_PATTERNS" not in source
        assert "is_shell_command_safe" in source
        assert "run_guarded_shell" in source

    def test_rm_rf_dot_is_blocked(self):
        """The old private list explicitly exempted this."""
        assert SafeShell.is_dangerous("rm -rf .") is not None

    def test_rm_rf_nested_path_is_allowed(self):
        assert SafeShell.is_dangerous("rm -rf ./build") is None

    def test_ordinary_command_is_allowed(self):
        assert SafeShell.is_dangerous("ls -la") is None

    def test_root_and_home_are_blocked(self):
        assert SafeShell.is_dangerous("rm -rf /") is not None
        assert SafeShell.is_dangerous("rm -rf ~") is not None

    def test_empty_is_dangerous(self):
        assert SafeShell.is_dangerous("   ") is not None

    def test_run_executes(self):
        result = SafeShell.run("echo guarded-ok")
        assert result.returncode == 0
        assert result.stdout.strip() == "guarded-ok"

    def test_run_respects_cwd(self, tmp_path):
        result = SafeShell.run("pwd", cwd=str(tmp_path))
        assert result.stdout.strip() == str(tmp_path.resolve())

    def test_run_blocks_dangerous(self):
        with pytest.raises(ValueError, match="blocked"):
            SafeShell.run("rm -rf .")

    def test_run_times_out(self):
        import subprocess

        with pytest.raises(subprocess.TimeoutExpired):
            SafeShell.run("sleep 5", timeout=1)

    def test_scrubs_secrets_from_the_child_env(self, monkeypatch):
        """exec_guard's scrubbing must be in effect, not bypassed."""
        monkeypatch.setenv("MY_SECRET_TOKEN", "hunter2")
        result = SafeShell.run("printenv MY_SECRET_TOKEN || true")
        assert "hunter2" not in result.stdout


# ---------------------------------------------------------------- utils/file
class TestFileOps:
    def test_atomic_write_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "a" / "b" / "c.txt"
        assert FileOps.atomic_write(str(target), "hello") is True
        assert target.read_text() == "hello"

    def test_atomic_write_overwrites(self, tmp_path):
        target = tmp_path / "f.txt"
        FileOps.atomic_write(str(target), "first")
        FileOps.atomic_write(str(target), "second")
        assert target.read_text() == "second"

    def test_no_temp_file_left_behind(self, tmp_path):
        target = tmp_path / "f.txt"
        FileOps.atomic_write(str(target), "content")
        assert list(tmp_path.iterdir()) == [target]

    def test_read_lines(self, tmp_path):
        target = tmp_path / "f.txt"
        target.write_text("one\ntwo\nthree\n")
        assert FileOps.read_lines(str(target)) == ["one", "two", "three"]

    def test_read_lines_missing_returns_empty(self, tmp_path):
        assert FileOps.read_lines(str(tmp_path / "nope.txt")) == []

    def test_ensure_dir(self, tmp_path):
        created = FileOps.ensure_dir(str(tmp_path / "x" / "y"))
        assert created.is_dir()

    def test_atomic_write_failure_returns_false(self, tmp_path):
        # A path whose parent is a file cannot be written through.
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        assert FileOps.atomic_write(str(blocker / "sub" / "f.txt"), "no") is False


# ------------------------------------------------------------- context_scout
class TestContextScout:
    def _project(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'demo'\ndependencies = ['httpx>=0.27.0', 'rich>=13']\n"
            "[project.optional-dependencies]\nnvim = ['pynvim>=0.4']\n",
            encoding="utf-8",
        )
        (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "main.py").write_text(
            "import asyncio\n\n\nasync def main():\n    await asyncio.sleep(0)\n",
            encoding="utf-8",
        )
        # build output must not be mistaken for project structure
        (tmp_path / "build" / "lib").mkdir(parents=True)
        (tmp_path / "build" / "lib" / "main.py").write_text("x = 1\n", encoding="utf-8")
        return tmp_path

    def test_detects_language(self, tmp_path):
        ctx = ContextScout(str(self._project(tmp_path))).scan()
        assert ctx.language == "Python"

    def test_reads_dependencies_from_pyproject(self, tmp_path):
        """pyproject.toml is the modern standard; ignoring it reported zero."""
        ctx = ContextScout(str(self._project(tmp_path))).scan()
        assert "httpx" in ctx.dependencies
        assert "rich" in ctx.dependencies

    def test_includes_optional_dependencies(self, tmp_path):
        ctx = ContextScout(str(self._project(tmp_path))).scan()
        assert "pynvim" in ctx.dependencies

    def test_version_specifiers_are_stripped(self, tmp_path):
        ctx = ContextScout(str(self._project(tmp_path))).scan()
        assert "httpx>=0.27.0" not in ctx.dependencies

    def test_build_artifacts_excluded_from_key_files(self, tmp_path):
        ctx = ContextScout(str(self._project(tmp_path))).scan()
        assert not any(f.startswith("build/") for f in ctx.key_files)
        assert "README.md" in ctx.key_files

    def test_reads_requirements_txt(self, tmp_path):
        (tmp_path / "requirements.txt").write_text(
            "flask==2.0\n# a comment\nrequests>=2\n", encoding="utf-8"
        )
        ctx = ContextScout(str(tmp_path)).scan()
        assert "flask" in ctx.dependencies
        assert "requests" in ctx.dependencies
        assert not any(d.startswith("#") for d in ctx.dependencies)

    def test_reads_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"dependencies": {"react": "^18"}, "devDependencies": {"vite": "^5"}}),
            encoding="utf-8",
        )
        ctx = ContextScout(str(tmp_path)).scan()
        assert "react" in ctx.dependencies
        assert "vite" in ctx.dependencies

    def test_malformed_pyproject_does_not_crash(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("this is not toml [[[", encoding="utf-8")
        ctx = ContextScout(str(tmp_path)).scan()
        assert isinstance(ctx.dependencies, list)

    def test_generates_agents_md(self, tmp_path):
        md = ContextScout(str(self._project(tmp_path))).generate_agents_md()
        assert md.startswith("# ")
        assert "## Key Dependencies" in md
        assert "httpx" in md

    def test_save_agents_md(self, tmp_path):
        root = self._project(tmp_path)
        written = ContextScout(str(root)).save_agents_md()
        assert written.name == "AGENTS.md"
        assert written.exists()
        assert written.read_text().startswith("# ")

    def test_init_project(self, tmp_path):
        from xli.core.context_scout import init_project

        root = self._project(tmp_path)
        assert init_project(str(root)).exists()


# --------------------------------------------------------------------- inbox
class TestInbox:
    @pytest.fixture
    def inbox(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        box = TeamInbox(project="proj", team="team")
        assert box.base_dir == tmp_path / ".xli" / "team_inbox" / "proj" / "team"
        return box

    def test_send_then_read_roundtrips(self, inbox):
        """The wire form uses from/to; constructing with **data raised."""
        asyncio.run(inbox.send("alice", "bob", "hello"))
        msgs = inbox.read_messages("bob")
        assert len(msgs) == 1
        assert msgs[0].from_agent == "alice"
        assert msgs[0].to_agent == "bob"
        assert msgs[0].text == "hello"

    def test_from_dict_maps_wire_keys(self):
        msg = InboxMessage.from_dict(
            {
                "id": "1",
                "from": "a",
                "to": "b",
                "text": "t",
                "timestamp": "ts",
                "team": "tm",
                "project": "pr",
            }
        )
        assert (msg.from_agent, msg.to_agent) == ("a", "b")

    def test_from_dict_accepts_field_names_too(self):
        msg = InboxMessage.from_dict(
            {"id": "1", "from_agent": "a", "to_agent": "b", "text": "t", "timestamp": "ts"}
        )
        assert msg.from_agent == "a"

    def test_multiple_messages_keep_order(self, inbox):
        for i in range(3):
            asyncio.run(inbox.send("alice", "bob", f"m{i}"))
        assert [m.text for m in inbox.read_messages("bob")] == ["m0", "m1", "m2"]

    def test_limit_returns_the_newest(self, inbox):
        for i in range(5):
            asyncio.run(inbox.send("a", "b", f"m{i}"))
        assert [m.text for m in inbox.read_messages("b", limit=2)] == ["m3", "m4"]

    def test_since_filters_older(self, inbox):
        asyncio.run(inbox.send("a", "b", "old"))
        assert inbox.read_messages("b", since="2999-01-01") == []
        assert len(inbox.read_messages("b", since="1999-01-01")) == 1

    def test_read_empty_inbox(self, inbox):
        assert inbox.read_messages("nobody") == []

    def test_torn_last_line_is_skipped(self, inbox):
        asyncio.run(inbox.send("a", "b", "good"))
        with open(inbox._get_inbox_path("b"), "a", encoding="utf-8") as f:
            f.write('{"id": "broken", "from": "a"')  # no newline, no close
        msgs = inbox.read_messages("b")
        assert [m.text for m in msgs] == ["good"]

    def test_broadcast_reaches_every_inbox_and_reports(self, inbox):
        asyncio.run(inbox.send("x", "bob", "seed"))
        asyncio.run(inbox.send("x", "carol", "seed"))

        sent = asyncio.run(inbox.broadcast("alice", "standup"))
        assert {m.to_agent for m in sent} == {"bob", "carol"}

    def test_broadcast_excludes_sender_and_listed(self, inbox):
        asyncio.run(inbox.send("x", "alice", "seed"))
        asyncio.run(inbox.send("x", "bob", "seed"))
        asyncio.run(inbox.send("x", "carol", "seed"))

        sent = asyncio.run(inbox.broadcast("alice", "hi", exclude=["carol"]))
        assert {m.to_agent for m in sent} == {"bob"}

    def test_broadcast_to_nobody_returns_empty(self, inbox):
        assert asyncio.run(inbox.broadcast("alice", "hello?")) == []

    def test_clear_inbox(self, inbox):
        asyncio.run(inbox.send("a", "b", "msg"))
        inbox.clear_inbox("b")
        assert inbox.read_messages("b") == []

    def test_to_dict_shape(self):
        msg = InboxMessage(
            id="1", from_agent="a", to_agent="b", text="t", timestamp="ts"
        )
        assert set(msg.to_dict()) == {"id", "from", "to", "text", "timestamp", "team", "project"}


# -------------------------------------------------------------- self_healing
class TestSelfHealing:
    def _engine(self, **kwargs):
        kwargs.setdefault("base_delay", 0.0)
        kwargs.setdefault("sleeper", lambda d: asyncio.sleep(0))
        return SelfHealingEngine(**kwargs)

    @pytest.mark.parametrize(
        "error,expected_retryable",
        [
            (ValueError("429 rate limited"), True),
            (RuntimeError("HTTP 503 service unavailable"), True),
            (TimeoutError("slow"), True),
            (ConnectionError("connection reset by peer"), True),
            (RuntimeError("too many requests"), True),
            (SyntaxError("bad indent"), False),
            (ValueError("401 invalid api key"), False),
            (PermissionError("permission denied"), False),
        ],
    )
    def test_classification(self, error, expected_retryable):
        """Message signals must beat the class name."""
        analysis = asyncio.run(self._engine().analyze_error(error))
        assert analysis.retryable is expected_retryable

    def test_generic_error_is_retryable(self):
        """Retrying with a modified approach is the point of the engine."""
        analysis = asyncio.run(self._engine().analyze_error(RuntimeError("boom")))
        assert analysis.retryable is True
        assert analysis.suggested_approach

    def test_fatal_is_critical(self):
        analysis = asyncio.run(self._engine().analyze_error(SyntaxError("x")))
        assert analysis.severity == "critical"

    def test_analysis_is_json_serialisable(self):
        """It goes into error_history, which is exposed over JSON-RPC."""
        analysis = asyncio.run(self._engine().analyze_error(RuntimeError("boom")))
        assert json.loads(json.dumps(analysis.to_dict()))["error_type"] == "RuntimeError"

    def test_heal_retries_until_success(self):
        calls = []

        async def attempt(task):
            calls.append(task)
            if len(calls) < 3:
                raise RuntimeError("still failing")
            return "recovered"

        engine = self._engine(max_retries=3)
        result = asyncio.run(engine.heal("do it", RuntimeError("boom"), attempt))
        assert result == "recovered"
        assert len(calls) == 3

    def test_heal_injects_the_error_into_the_retry(self):
        seen = []

        async def attempt(task):
            seen.append(task)
            return "ok"

        asyncio.run(self._engine().heal("original", RuntimeError("the failure"), attempt))
        assert "[PREVIOUS ERROR]: the failure" in seen[0]

    def test_heal_raises_when_exhausted(self):
        async def attempt(task):
            raise RuntimeError("never works")

        engine = self._engine(max_retries=2)
        with pytest.raises(RuntimeError):
            asyncio.run(engine.heal("do it", RuntimeError("boom"), attempt))
        # history must be serialisable
        assert json.dumps(engine.error_history)

    def test_heal_does_not_retry_fatal(self):
        calls = []

        async def attempt(task):
            calls.append(task)
            return "ok"

        with pytest.raises(SyntaxError):
            asyncio.run(self._engine().heal("do it", SyntaxError("bad"), attempt))
        assert calls == []

    def test_retry_with_backoff_succeeds_late(self):
        state = {"n": 0}

        async def flaky():
            state["n"] += 1
            if state["n"] < 3:
                raise RuntimeError("nope")
            return "finally"

        assert asyncio.run(self._engine(max_retries=3).retry_with_backoff(flaky)) == "finally"

    def test_retry_with_backoff_raises_when_exhausted(self):
        async def always():
            raise RuntimeError("nope")

        with pytest.raises(RuntimeError):
            asyncio.run(self._engine(max_retries=2).retry_with_backoff(always))

    def test_retry_with_backoff_accepts_sync_functions(self):
        assert asyncio.run(self._engine().retry_with_backoff(lambda: 42)) == 42

    def test_sleeper_is_actually_used(self):
        slept = []

        async def record(delay):
            slept.append(delay)

        state = {"n": 0}

        async def flaky():
            state["n"] += 1
            if state["n"] < 2:
                raise RuntimeError("nope")
            return "ok"

        engine = SelfHealingEngine(max_retries=3, base_delay=1.0, sleeper=record)
        asyncio.run(engine.retry_with_backoff(flaky))
        assert len(slept) == 1

    def test_singleton(self):
        reset_healing_engine()
        assert get_healing_engine() is get_healing_engine()
        reset_healing_engine()


# --------------------------------------------------- agent + healing integration
class TestAgentHealing:
    class FlakyProvider:
        def __init__(self, fail_times, exc):
            self.fail_times = fail_times
            self.exc = exc
            self.calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls <= self.fail_times:
                raise self.exc
            return "<done>recovered</done>"

    def _agent(self, provider, healer=None):
        from xli.agent import Agent
        from xli.permissions.policy import Mode, Policy

        return Agent(provider, policy=Policy(mode=Mode.AUTO), healer=healer)

    def test_transient_failure_recovers_with_a_healer(self):
        provider = self.FlakyProvider(2, RuntimeError("HTTP 503 service unavailable"))
        engine = SelfHealingEngine(
            max_retries=3, base_delay=0.0, sleeper=lambda d: asyncio.sleep(0)
        )
        result = asyncio.run(self._agent(provider, engine).run("go"))
        assert result.ok is True
        assert provider.calls == 3

    def test_without_a_healer_it_stops_immediately(self):
        provider = self.FlakyProvider(2, RuntimeError("HTTP 503 service unavailable"))
        result = asyncio.run(self._agent(provider).run("go"))
        assert result.ok is False
        assert result.stopped_reason == "provider_error"
        assert provider.calls == 1

    def test_fatal_failure_is_not_retried(self):
        provider = self.FlakyProvider(9, ValueError("401 invalid api key"))
        engine = SelfHealingEngine(
            max_retries=3, base_delay=0.0, sleeper=lambda d: asyncio.sleep(0)
        )
        result = asyncio.run(self._agent(provider, engine).run("go"))
        assert result.stopped_reason == "provider_error"
        assert provider.calls == 1

    def test_healer_defaults_to_none(self):
        from xli.agent import Agent
        from xli.permissions.policy import Mode, Policy
        from xli.providers.fake import FakeProvider

        assert Agent(FakeProvider(), policy=Policy(mode=Mode.AUTO)).healer is None


# ----------------------------------------------------------------------- rpc
class TestKernelMethods:
    def _call(self, kernel, method, params=None):
        from xli.kernel.protocol import Request, encode_line

        out = asyncio.run(kernel.feed_line(encode_line(Request(method=method, id=1, params=params or {}))))
        return out[0]

    def test_context_scout(self, tmp_path):
        from xli.kernel.methods import build_kernel

        (tmp_path / "pyproject.toml").write_text(
            "[project]\ndependencies = ['httpx>=1']\n", encoding="utf-8"
        )
        kernel = build_kernel(project_root=tmp_path)
        result = self._call(kernel, "context.scout")
        assert result.error is None
        assert "httpx" in result.result["dependencies"]

    def test_context_scout_explicit_path(self, tmp_path):
        from xli.kernel.methods import build_kernel

        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "package.json").write_text('{"dependencies":{"react":"^18"}}', encoding="utf-8")
        kernel = build_kernel(project_root=tmp_path)
        result = self._call(kernel, "context.scout", {"path": str(sub)})
        assert "react" in result.result["dependencies"]

    def test_context_agents_md_generates_without_saving(self, tmp_path):
        from xli.kernel.methods import build_kernel

        kernel = build_kernel(project_root=tmp_path)
        result = self._call(kernel, "context.agents_md")
        assert result.result["saved"] is False
        assert result.result["content"]
        assert not (tmp_path / "AGENTS.md").exists()

    def test_context_agents_md_saves(self, tmp_path):
        from xli.kernel.methods import build_kernel

        kernel = build_kernel(project_root=tmp_path)
        result = self._call(kernel, "context.agents_md", {"save": True})
        assert result.result["saved"] is True
        assert (tmp_path / "AGENTS.md").exists()

    def test_inbox_roundtrip(self, tmp_path, monkeypatch):
        from xli.kernel.methods import build_kernel

        monkeypatch.setenv("HOME", str(tmp_path))
        kernel = build_kernel(project_root=tmp_path)

        sent = self._call(kernel, "inbox.send", {"sender": "a", "recipient": "b", "text": "hi"})
        assert sent.error is None
        assert sent.result["message"]["from"] == "a"

        read = self._call(kernel, "inbox.read", {"agent": "b"})
        assert read.result["count"] == 1

    def test_inbox_read_requires_agent(self, tmp_path):
        from xli.kernel.methods import build_kernel

        kernel = build_kernel(project_root=tmp_path)
        assert self._call(kernel, "inbox.read", {}).error is not None

    def test_heal_analyze_classifies(self, tmp_path):
        from xli.kernel.methods import build_kernel

        kernel = build_kernel(project_root=tmp_path)
        assert self._call(kernel, "heal.analyze", {"error": "429 rate limited"}).result[
            "retryable"
        ]
        assert not self._call(kernel, "heal.analyze", {"error": "401 invalid api key"}).result[
            "retryable"
        ]

    def test_heal_history_is_serialisable(self, tmp_path):
        from xli.kernel.methods import build_kernel

        kernel = build_kernel(project_root=tmp_path)
        result = self._call(kernel, "heal.history")
        assert json.dumps(result.result)

    def test_new_methods_are_listed(self, tmp_path):
        from xli.kernel.methods import build_kernel

        kernel = build_kernel(project_root=tmp_path)
        names = {m["name"] for m in self._call(kernel, "rpc.methods").result["methods"]}
        assert {
            "context.scout",
            "context.agents_md",
            "inbox.send",
            "inbox.read",
            "inbox.broadcast",
            "inbox.status",
            "heal.analyze",
            "heal.history",
        } <= names


# ----------------------------------------------------------------------- cli
class TestCli:
    def _run(self, argv, cwd=None):
        import io
        import contextlib

        from xli.cli import main

        out, err = io.StringIO(), io.StringIO()
        old = os.getcwd()
        try:
            if cwd:
                os.chdir(cwd)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(argv)
        finally:
            os.chdir(old)
        return code, out.getvalue(), err.getvalue()

    def test_scout_reports(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[project]\ndependencies = ['httpx>=1']\n", encoding="utf-8"
        )
        code, out, _ = self._run(["scout", str(tmp_path)])
        assert code == 0
        assert "httpx" in out

    def test_scout_json(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[project]\ndependencies = ['httpx>=1']\n", encoding="utf-8"
        )
        code, out, _ = self._run(["scout", str(tmp_path), "--json"])
        assert code == 0
        assert "httpx" in json.loads(out)["dependencies"]

    def test_scout_save(self, tmp_path):
        code, out, _ = self._run(["scout", str(tmp_path), "--save"])
        assert code == 0
        assert (tmp_path / "AGENTS.md").exists()

    def test_scout_missing_dir(self, tmp_path):
        code, _, err = self._run(["scout", str(tmp_path / "nope")])
        assert code == 2
        assert "no such directory" in err

    def test_inbox_send_and_read(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        code, out, _ = self._run(
            ["inbox", "send", "--sender", "a", "--recipient", "b", "--text", "hello"]
        )
        assert code == 0
        code, out, _ = self._run(["inbox", "read", "--recipient", "b"])
        assert code == 0
        assert "hello" in out

    def test_inbox_send_requires_fields(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        code, _, err = self._run(["inbox", "send"])
        assert code == 2
        assert "usage" in err

    def test_inbox_status_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        code, out, _ = self._run(["inbox", "status"])
        assert code == 0
        assert "no agents yet" in out

    def test_inbox_json_is_parseable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        self._run(["inbox", "send", "--sender", "a", "--recipient", "b", "--text", "x"])
        code, out, _ = self._run(["inbox", "status", "--json"])
        assert code == 0
        assert json.loads(out)["agents"][0]["agent"] == "b"
