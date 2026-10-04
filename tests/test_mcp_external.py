#!/usr/bin/env python3
"""External MCP servers: reading Claude/Codex config, launching, talking.

The client used to be able to launch exactly one thing — a bundled server, as
`python -m xli.mcp.servers.<name>`. Everything else was configuration XLI
could not use: a `.mcp.json` written for Claude Code, a Codex `config.toml`,
a hosted endpoint. These tests run against a real child process that is
awkward on purpose (banner on stdout, notifications before replies, errors).

Nothing here needs the network or `npx`.
"""

import json
import sys
import textwrap
from pathlib import Path

import pytest

from xli.mcp.client import MCPClient, MCPError
from xli.mcp.config import discover, allowed_tools
from xli.mcp.registry import MCPRegistry
from xli.mcp.transport import StdioTransport, TransportError, _iter_replies

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"
COMMAND = [sys.executable, str(FIXTURE)]


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """A project with a Claude-style `.mcp.json` and a Codex config."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    write(
        tmp_path / ".mcp.json",
        json.dumps(
            {
                "mcpServers": {
                    "fake": {"command": COMMAND[0], "args": [COMMAND[1]]},
                    "remote": {"type": "http", "url": "https://example.invalid/mcp"},
                    "switched-off": {"command": "true", "enabled": False},
                    "with-env": {
                        "command": COMMAND[0],
                        "args": [COMMAND[1]],
                        "env": {"FAKE_TOKEN": "secret"},
                        "enabled_tools": ["echo"],
                        "tool_timeout_sec": 30,
                    },
                }
            }
        ),
    )
    write(
        tmp_path / "home" / ".codex" / "config.toml",
        """
        # Codex keeps its servers in TOML
        [mcp_servers.codex_one]
        command = "npx"
        args = ["-y", "@some/server"]
        startup_timeout_sec = 12
        tool_timeout_sec = 90
        enabled = true
        disabled_tools = ["dangerous"]
        """,
    )
    return tmp_path


class TestConfigDiscovery:
    def test_claude_project_file_is_read(self, project):
        found = discover(start=project)
        assert found["fake"].command == COMMAND
        assert found["fake"].source.endswith(".mcp.json")

    def test_http_endpoints_are_recognised(self, project):
        remote = discover(start=project)["remote"]
        assert remote.kind == "http"
        assert remote.url == "https://example.invalid/mcp"

    def test_a_disabled_server_stays_disabled(self, project):
        assert discover(start=project)["switched-off"].enabled is False

    def test_codex_toml_is_read(self, project):
        codex = discover(start=project)["codex_one"]
        assert codex.command == ["npx", "-y", "@some/server"]
        assert codex.tool_timeout == 90.0
        assert codex.startup_timeout == 12.0
        assert codex.disabled_tools == ("dangerous",)

    def test_nearest_project_file_wins(self, project):
        nested = project / "src" / "deep"
        nested.mkdir(parents=True)
        (project / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"outer": {"command": "outer-cmd"}}}), encoding="utf-8"
        )
        (nested / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"inner": {"command": "inner-cmd"}}}), encoding="utf-8"
        )
        found = discover(start=nested)
        assert "inner" in found and "outer" not in found

    def test_tool_filters_are_applied(self, project):
        info = discover(start=project)["with-env"].to_info()
        assert allowed_tools(info, ["echo", "other"]) == ["echo"]
        assert allowed_tools({"disabled_tools": ["x"]}, ["x", "y"]) == ["y"]

    def test_a_broken_file_is_skipped_not_fatal(self, project):
        (project / ".mcp.json").write_text("{not json", encoding="utf-8")
        assert discover(start=project)  # the Codex file is still read

    def test_entries_without_a_command_are_ignored(self, project):
        (project / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"nothing": {"description": "no command"}}}), encoding="utf-8"
        )
        assert "nothing" not in discover(start=project)


class TestRegistryIntegration:
    def test_external_servers_are_visible(self, project, monkeypatch):
        monkeypatch.chdir(project)
        registry = MCPRegistry.__new__(MCPRegistry)  # bypass the singleton cache
        registry._initialized = True
        registry.servers = {}
        registry.external = {}
        registry._config = None
        registry._load_external()
        assert "fake" in registry.servers
        assert registry.is_external("fake")
        assert registry.command_for("fake") == COMMAND
        assert registry.url_for("remote").startswith("https://")

    def test_a_disabled_external_server_is_not_enabled_by_default(self, project, monkeypatch):
        monkeypatch.chdir(project)
        registry = MCPRegistry.__new__(MCPRegistry)
        registry._initialized = True
        registry.servers = {}
        registry.external = {}
        registry._config = None
        registry._load_external()
        registry._apply_config(None)
        assert registry.is_enabled("fake")
        assert not registry.is_enabled("switched-off")


class TestTalkingToARealProcess:
    def test_probe_starts_it_and_lists_tools(self, project, monkeypatch):
        monkeypatch.chdir(project)
        with MCPClient() as client:
            report = client.probe("fake")
        assert report["tool_count"] == 2
        assert "echo" in report["tools"]
        assert report["source"].endswith(".mcp.json")

    def test_a_call_returns_the_servers_result(self, project, monkeypatch):
        import asyncio

        monkeypatch.chdir(project)
        with MCPClient() as client:
            result = asyncio.run(client.call_tool("fake", "echo", {"text": "привет"}))
        assert result["content"][0]["text"] == "echo: привет"

    def test_a_server_error_is_raised_with_its_message(self, project, monkeypatch):
        import asyncio

        monkeypatch.chdir(project)
        with MCPClient() as client:
            with pytest.raises(MCPError, match="boom, as requested"):
                asyncio.run(client.call_tool("fake", "boom", {}))

    def test_enabled_tools_filters_what_the_server_offers(self, project, monkeypatch):
        monkeypatch.chdir(project)
        import asyncio

        with MCPClient() as client:
            tools = asyncio.run(client.list_tools("with-env"))
        assert [tool["name"] for tool in tools] == ["echo"]

    def test_a_missing_command_is_a_clear_error(self, project, monkeypatch):
        monkeypatch.chdir(project)
        (project / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"ghost": {"command": "definitely-not-installed-xyz"}}}),
            encoding="utf-8",
        )
        from xli.mcp.config import ExternalServer

        client = MCPClient()
        client._transports["ghost"] = StdioTransport(["definitely-not-installed-xyz"])
        with pytest.raises((TransportError, MCPError), match="not found|no such file|No such file"):
            client.probe("ghost")


class TestTransportRobustness:
    def test_a_banner_before_the_handshake_does_not_break_it(self, project, monkeypatch):
        """The fixture prints a non-JSON banner on stdout at startup."""
        monkeypatch.chdir(project)
        client = MCPClient()
        transport = client._transport("fake")
        reply = transport.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        assert len(reply["result"]["tools"]) == 2
        client.close()

    def test_a_hang_is_an_error_not_an_eternity(self):
        # The handshake is the first thing that has to time out, so the
        # startup timeout is the one that has to be short here.
        transport = StdioTransport(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.5,
            startup_timeout=0.5,
        )
        with pytest.raises(TransportError, match="no reply"):
            transport.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        transport.close()

    def test_a_server_that_exits_is_reported_with_its_output(self):
        transport = StdioTransport(
            [sys.executable, "-c", "import sys; sys.stderr.write('nope\\n'); sys.exit(3)"],
            timeout=2,
        )
        with pytest.raises(TransportError, match="nope"):
            transport.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        transport.close()

    def test_sse_shaped_bodies_are_read(self):
        body = 'event: message\ndata: {"jsonrpc": "2.0", "id": 7, "result": {"ok": true}}\n\n'
        replies = _iter_replies(body)
        assert replies == [{"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}]

    def test_a_plain_json_body_is_read(self):
        assert _iter_replies('{"jsonrpc": "2.0", "id": 1, "result": {}}')[0]["id"] == 1
