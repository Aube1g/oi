#!/usr/bin/env python3
"""Tests for xli.paths — one definition of where xli keeps its state.

Every module used to compute `Path.home() / ".xli" / <something>` for itself.
Only Config.user_path() consulted XLI_CONFIG_DIR, so the override moved the
config file and nothing else: cache.db, memory.db, skills.db, snapshots/,
queue/, prompts/, xpi_state.json, the XPI plugin directory and the kernel socket
all stayed in the real home directory. Measured before the fix: with
XLI_CONFIG_DIR set, 5 of 5 sampled paths still resolved under $HOME/.xli.

The consequence is worse than untidiness. Two invocations pointed at different
config directories silently shared a cache, a skills index and a memory
database, so one project's state could surface in another's.
"""

import importlib
import os
import subprocess
import sys

import pytest

from xli.paths import DIRNAME, ENV_VAR, xli_home, xli_path


class TestResolver:
    def test_defaults_to_home_dot_xli(self, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        from pathlib import Path

        assert xli_home() == Path.home() / DIRNAME

    def test_honours_the_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ENV_VAR, str(tmp_path))
        assert xli_home() == tmp_path

    def test_expands_a_tilde_in_the_override(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "~/somewhere-else")
        assert "~" not in str(xli_home())

    def test_xli_path_joins(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ENV_VAR, str(tmp_path))
        assert xli_path("cache.db") == tmp_path / "cache.db"
        assert xli_path("team_inbox", "proj", "team") == tmp_path / "team_inbox" / "proj" / "team"

    def test_the_resolver_is_read_at_call_time(self, monkeypatch, tmp_path):
        """A later override must be visible, not frozen at import."""
        monkeypatch.setenv(ENV_VAR, str(tmp_path / "a"))
        first = xli_home()
        monkeypatch.setenv(ENV_VAR, str(tmp_path / "b"))
        assert xli_home() != first
        assert xli_home() == tmp_path / "b"

    def test_it_imports_nothing_from_xli(self):
        """xli.core.logger is pulled in by nearly everything; a cycle here is fatal."""
        code = (
            "import sys, xli.paths; "
            "print([m for m in sys.modules if m.startswith('xli.') and m != 'xli.paths'])"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "[]", f"importing xli.paths pulled in {out.stdout.strip()}"


class TestEveryPathHonoursTheOverride:
    """Checked in a subprocess, so each path resolves in a fresh interpreter.

    Most of these are module-level constants, frozen at import; snapshots_dir()
    is a function, which is why the probe calls whatever is callable.
    """

    MODULE_ATTRS = [
        ("xli.core.cache", "CACHE_DB"),
        ("xli.core.memory", "MEMORY_DB"),
        ("xli.core.skills", "SKILLS_DB"),
        ("xli.core.skills", "SKILLS_DIR"),
        ("xli.core.prompt_lab", "PROMPT_DIR"),
        ("xli.core.queue", "QUEUE_DIR"),
        ("xli.core.time_machine", "snapshots_dir"),
        ("xli.xpi.state", "STATE_FILE"),
        ("xli.xpi.manager", "XPI_DIR"),
        ("xli.mcp.manager", "XPI_DIR"),
        ("xli.mcp.servers.prompt", "STORAGE"),
        ("xli.kernel.daemon", "DEFAULT_SOCKET_DIR"),
    ]

    def test_all_of_them_move(self, tmp_path):
        env = dict(os.environ)
        env[ENV_VAR] = str(tmp_path)
        env["PYTHONPATH"] = str(importlib.import_module("xli").__path__[0].rsplit("/xli", 1)[0])

        # Real newlines: `def` cannot be joined onto one line with semicolons.
        probe = (
            "import importlib, json\n"
            "pairs = " + repr(self.MODULE_ATTRS) + "\n"
            "def resolve(m, a):\n"
            "    v = getattr(importlib.import_module(m), a)\n"
            "    return str(v() if callable(v) else v)\n"
            "print(json.dumps({f'{m}.{a}': resolve(m, a) for m, a in pairs}))\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120, env=env
        )
        assert out.returncode == 0, out.stderr

        import json

        resolved = json.loads(out.stdout.strip().splitlines()[-1])
        offenders = {k: v for k, v in resolved.items() if not v.startswith(str(tmp_path))}
        assert not offenders, f"these ignore {ENV_VAR}: {offenders}"

    def test_the_runtime_dir_override_still_wins_for_the_socket(self, tmp_path):
        """XLI_RUNTIME_DIR is a deliberate separate knob for a runtime artefact."""
        env = dict(os.environ)
        env[ENV_VAR] = str(tmp_path / "config")
        env["XLI_RUNTIME_DIR"] = str(tmp_path / "runtime")
        env["PYTHONPATH"] = str(importlib.import_module("xli").__path__[0].rsplit("/xli", 1)[0])

        out = subprocess.run(
            [
                sys.executable,
                "-c",
                "from xli.kernel.daemon import DEFAULT_SOCKET_DIR; print(DEFAULT_SOCKET_DIR)",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == str(tmp_path / "runtime")

    def test_no_module_hardcodes_the_home_path(self):
        """Greps the tree, so a reintroduced literal is caught."""
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "xli"
        pattern = re.compile(r'Path\.home\(\)\s*/\s*"\.xli"')
        offenders = []
        for path in root.rglob("*.py"):
            if "__pycache__" in str(path) or path.name == "paths.py":
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line) and not line.lstrip().startswith("#"):
                    offenders.append(f"{path}:{lineno}: {line.strip()}")
        assert not offenders, "hardcoded ~/.xli paths:\n" + "\n".join(offenders)


class TestLoggerDegradesInsteadOfRaising:
    def test_an_unwritable_log_directory_does_not_break_the_logger(self, tmp_path):
        """The regression this exposed: __init__ did an unconditional mkdir.

        Safe only while ~/.xli is writable. Point XLI_CONFIG_DIR somewhere it
        cannot create and the constructor raised, taking the process with it.
        """
        env = dict(os.environ)
        env[ENV_VAR] = "/nonexistent-config-dir"
        env["PYTHONPATH"] = str(importlib.import_module("xli").__path__[0].rsplit("/xli", 1)[0])

        out = subprocess.run(
            [
                sys.executable,
                "-c",
                "from xli.core.logger import StructuredLogger;"
                "log = StructuredLogger('probe');"
                "log.log_structured('INFO', 'probe', 'still alive');"
                "print('OK', log.file_logging)",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert out.returncode == 0, f"logger raised: {out.stderr}"
        assert out.stdout.strip().startswith("OK False")

    def test_file_logging_is_on_when_the_directory_is_writable(self, tmp_path):
        env = dict(os.environ)
        env[ENV_VAR] = str(tmp_path)
        env["PYTHONPATH"] = str(importlib.import_module("xli").__path__[0].rsplit("/xli", 1)[0])

        out = subprocess.run(
            [
                sys.executable,
                "-c",
                "from xli.core.logger import StructuredLogger;"
                "log = StructuredLogger('probe');"
                "log.log_structured('INFO', 'probe', 'written');"
                "print('OK', log.file_logging)",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip().startswith("OK True")
        assert (tmp_path / "logs" / "structured.log").is_file()

    def test_a_write_failure_disables_structured_logging_once(self, monkeypatch, tmp_path):
        """Not once per call — otherwise every log line emits an error."""
        from xli.core.logger import StructuredLogger

        log = StructuredLogger(f"probe-{tmp_path.name}")
        log.structured_path = tmp_path / "gone" / "structured.log"  # parent missing

        with pytest.MonkeyPatch.context() as mp:
            seen = []
            mp.setattr(log.logger, "warning", lambda msg, *a, **k: seen.append(msg))
            log.log_structured("INFO", "probe", "one")
            log.log_structured("INFO", "probe", "two")

        assert log.structured_path is None
        assert len(seen) == 1, "the failure should be reported once, not per call"
