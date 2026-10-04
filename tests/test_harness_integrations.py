"""XLI as a guest in Claude Code and Codex.

These files are the user's own: a `.mcp.json` that already names three servers,
a `config.toml` full of settings and comments. So most of what is tested here
is the merge — that installing XLI leaves everything else exactly as it was,
that running it twice is a no-op, and that `--force` only replaces XLI's own
entry.

The last test is the interesting one: XLI's *readers* must be able to read what
the writers wrote. If `xli mcp list` cannot see the server the installer added,
the integration is decoration.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from xli.claude.install import install as claude_install
from xli.claude.install import load_plugin_manifest, plugin_files
from xli.codex.install import install as codex_install
from xli.harness import MARK_END, MARK_START


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


class TestClaudeInstall:
    def test_the_server_lands_in_the_project_file(self, project):
        report = claude_install(project=project)
        data = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
        assert data["mcpServers"]["xli"] == {"command": "xli", "args": ["mcp", "serve"]}
        assert str(project / ".mcp.json") in report.written

    def test_existing_servers_are_untouched(self, project):
        existing = {
            "mcpServers": {
                "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]},
                "notion": {"url": "https://mcp.notion.com/sse", "transport": "sse"},
            },
            "someOtherKey": {"keep": True},
        }
        (project / ".mcp.json").write_text(json.dumps(existing), encoding="utf-8")
        claude_install(project=project)

        data = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
        assert data["mcpServers"]["github"] == existing["mcpServers"]["github"]
        assert data["mcpServers"]["notion"] == existing["mcpServers"]["notion"]
        assert data["someOtherKey"] == {"keep": True}
        assert data["mcpServers"]["xli"]["args"] == ["mcp", "serve"]

    def test_a_second_install_changes_nothing(self, project):
        claude_install(project=project)
        before = {
            path.name: path.read_text(encoding="utf-8")
            for path in project.rglob("*")
            if path.is_file()
        }
        report = claude_install(project=project)
        after = {
            path.name: path.read_text(encoding="utf-8")
            for path in project.rglob("*")
            if path.is_file()
        }
        assert before == after
        assert report.written == []
        assert len(report.unchanged) == 3

    def test_a_hand_edited_entry_survives_without_force(self, project):
        data = {"mcpServers": {"xli": {"command": "/opt/venv/bin/xli", "args": ["mcp", "serve"]}}}
        (project / ".mcp.json").write_text(json.dumps(data), encoding="utf-8")
        claude_install(project=project)
        kept = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
        assert kept["mcpServers"]["xli"]["command"] == "/opt/venv/bin/xli"

    def test_force_replaces_xli_entry_only(self, project):
        data = {
            "mcpServers": {
                "xli": {"command": "/opt/venv/bin/xli", "args": ["mcp", "serve"]},
                "other": {"command": "other"},
            }
        }
        (project / ".mcp.json").write_text(json.dumps(data), encoding="utf-8")
        claude_install(project=project, force=True)
        changed = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
        assert changed["mcpServers"]["xli"]["command"] == "xli"
        assert changed["mcpServers"]["other"] == {"command": "other"}

    def test_a_broken_json_file_is_reported_not_destroyed(self, project):
        (project / ".mcp.json").write_text("{это не json", encoding="utf-8")
        report = claude_install(project=project)
        assert (project / ".mcp.json").read_text(encoding="utf-8") == "{это не json"
        assert any("не разобрать" in message for message in report.messages)

    def test_the_command_file_is_a_real_slash_command(self, project):
        claude_install(project=project)
        text = (project / ".claude" / "commands" / "xli.md").read_text(encoding="utf-8")
        assert text.startswith("---")
        assert "xli run" in text and "$ARGUMENTS" in text
        assert "Bash(xli:*)" in text
        assert any("А" <= char <= "я" for char in text), "команда не на русском"

    def test_the_memory_block_is_marked_and_added_once(self, project):
        memory = project / "CLAUDE.md"
        memory.write_text("# Мой проект\n\nчто-то важное\n", encoding="utf-8")
        claude_install(project=project)
        claude_install(project=project)
        text = memory.read_text(encoding="utf-8")
        assert text.startswith("# Мой проект")
        assert text.count(MARK_START) == 1 and text.count(MARK_END) == 1
        assert "XLI" in text

    def test_user_scope_writes_to_the_home_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
        claude_install(user=True)
        assert (tmp_path / "home" / ".claude.json").exists()
        assert (tmp_path / "home" / ".claude" / "commands" / "xli.md").exists()

    def test_the_plugin_tree_can_be_installed(self, project, tmp_path):
        target = tmp_path / "plugins" / "xli"
        report = claude_install(project=project, plugin_dir=target)
        assert (target / ".claude-plugin" / "plugin.json").exists()
        assert (target / "commands" / "xli.md").exists()
        assert (target / ".mcp.json").exists()
        assert report.written
        # Second copy is a no-op, like the rest of the installer.
        again = claude_install(project=project, plugin_dir=target)
        assert not [path for path in again.written if "plugins" in path]


class TestClaudePlugin:
    def test_the_manifest_is_valid_and_points_at_the_commands(self):
        manifest = load_plugin_manifest()
        assert manifest["name"] == "xli"
        assert manifest["commands"] == "./commands"
        assert manifest["mcpServers"] == "./.mcp.json"

    def test_the_shipped_mcp_file_matches_what_the_installer_writes(self):
        from xli.claude.install import PLUGIN_SOURCE
        from xli.harness import mcp_entry

        shipped = json.loads((PLUGIN_SOURCE / ".mcp.json").read_text(encoding="utf-8"))
        assert shipped["mcpServers"]["xli"] == mcp_entry()

    def test_the_skill_is_russian_and_about_charts(self):
        skill = (
            Path(__file__).resolve().parents[1]
            / "xli" / "claude" / "plugin_root" / "skills" / "xli-charts" / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert "chart" in skill
        assert "spark" in skill and "heat" in skill
        assert any("А" <= char <= "я" for char in skill)

    def test_every_plugin_file_is_accounted_for(self):
        files = plugin_files()
        assert ".claude-plugin/plugin.json" in files
        assert ".mcp.json" in files
        assert len([name for name in files if name.startswith("commands/")]) == 3


class TestCodexInstall:
    def test_the_config_gets_a_real_toml_section(self, tmp_path):
        home = tmp_path / "codex"
        report = codex_install(home=home)
        text = (home / "config.toml").read_text(encoding="utf-8")
        parsed = tomllib.loads(text)
        assert parsed["mcp_servers"]["xli"]["command"] == "xli"
        assert parsed["mcp_servers"]["xli"]["args"] == ["mcp", "serve"]
        assert parsed["mcp_servers"]["xli"]["tool_timeout_sec"] == 120
        assert str(home / "config.toml") in report.written

    def test_the_users_own_settings_and_comments_survive(self, tmp_path):
        home = tmp_path / "codex"
        home.mkdir()
        original = (
            '# мои настройки\n'
            'model = "gpt-5-codex"\n\n'
            '[mcp_servers.filesystem]\n'
            'command = "npx"\n'
            'args = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]\n'
        )
        (home / "config.toml").write_text(original, encoding="utf-8")
        codex_install(home=home)

        text = (home / "config.toml").read_text(encoding="utf-8")
        assert "# мои настройки" in text
        assert 'model = "gpt-5-codex"' in text
        parsed = tomllib.loads(text)
        assert "filesystem" in parsed["mcp_servers"]
        assert "xli" in parsed["mcp_servers"]

    def test_a_second_install_changes_nothing(self, tmp_path):
        home = tmp_path / "codex"
        codex_install(home=home)
        before = (home / "config.toml").read_text(encoding="utf-8")
        report = codex_install(home=home)
        assert (home / "config.toml").read_text(encoding="utf-8") == before
        assert report.written == []

    def test_force_replaces_only_our_section(self, tmp_path):
        home = tmp_path / "codex"
        home.mkdir()
        (home / "config.toml").write_text(
            '[mcp_servers.other]\ncommand = "other"\n\n'
            '[mcp_servers.xli]\ncommand = "/old/xli"\nargs = ["mcp", "serve"]\n',
            encoding="utf-8",
        )
        codex_install(home=home, force=True)
        parsed = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
        assert parsed["mcp_servers"]["xli"]["command"] == "xli"
        assert parsed["mcp_servers"]["other"]["command"] == "other"

    def test_the_prompt_is_written(self, tmp_path):
        home = tmp_path / "codex"
        codex_install(home=home)
        prompt = (home / "prompts" / "xli.md").read_text(encoding="utf-8")
        assert "$ARGUMENTS" in prompt
        assert "xli run" in prompt

    def test_the_project_instructions_are_only_touched_when_asked(self, tmp_path, project):
        codex_install(home=tmp_path / "codex")
        assert not (project / "AGENTS.md").exists()
        codex_install(home=tmp_path / "codex", project=project)
        assert "XLI" in (project / "AGENTS.md").read_text(encoding="utf-8")

    def test_codex_home_is_honoured(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom"))
        from xli.codex.install import codex_home

        assert codex_home() == tmp_path / "custom"


class TestXliCanReadWhatItWrote:
    """The compatibility claim, checked end to end."""

    def test_the_claude_installer_writes_something_xli_reads_back(self, project):
        claude_install(project=project)
        from xli.mcp.config import discover

        found = discover(start=project)
        assert "xli" in found
        server = found["xli"]
        assert server.command == ["xli", "mcp", "serve"]
        assert server.source.endswith(".mcp.json")

    def test_the_codex_installer_writes_something_xli_reads_back(self, tmp_path):
        home = tmp_path / "codex"
        codex_install(home=home)
        from xli.mcp.config import discover

        # discover() looks at ~/.codex/config.toml, so point it at ours.
        import os

        previous = os.environ.get("HOME")
        os.environ["HOME"] = str(tmp_path)
        try:
            (tmp_path / ".codex").mkdir(exist_ok=True)
            (tmp_path / ".codex" / "config.toml").write_text(
                (home / "config.toml").read_text(encoding="utf-8"), encoding="utf-8"
            )
            found = discover(start=tmp_path)
        finally:
            if previous is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous
        assert "xli" in found
        # `command` is the whole argv, the way a launcher needs it.
        assert found["xli"].command == ["xli", "mcp", "serve"]
        assert found["xli"].tool_timeout == 120.0


class TestCommandLine:
    def run(self, *args: str, home: Path):
        import os
        import subprocess
        import sys

        environment = dict(os.environ)
        environment["HOME"] = str(home)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        return subprocess.run(
            [sys.executable, "-m", "xli", *args],
            capture_output=True, text=True, env=environment, timeout=200,
        )

    def test_claude_install_reports_in_json(self, tmp_path):
        result = self.run("claude", "install", "--project", str(tmp_path / "p"),
                          "--json", home=tmp_path / "home")
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert any(path.endswith(".mcp.json") for path in payload["written"])

    def test_codex_show_says_where_the_files_live(self, tmp_path):
        result = self.run("codex", "show", "--home", str(tmp_path / "codex"),
                          "--json", home=tmp_path / "home")
        payload = json.loads(result.stdout)
        assert payload["config"].endswith("config.toml")
        assert payload["prompt"].endswith("xli.md")

    def test_the_help_mentions_both_integrations(self, tmp_path):
        result = self.run("--help", home=tmp_path / "home")
        assert "claude" in result.stdout and "codex" in result.stdout
