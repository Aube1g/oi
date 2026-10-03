#!/usr/bin/env python3
"""Tests for the MCP client and the registry's tool catalogue.

The client used to be a placeholder: it logged the call and returned
{"status": "ok", "result": f"MCP {server}.{tool} called"} without starting a
process, and list_tools() returned []. Every MCP call therefore "succeeded"
while doing nothing, so the agent received invented context with no way to
tell.

The registry also advertised tool names that the real servers do not have —
17 of 18 entries had a tools[0] that did not exist in the server, and MCPBridge
always calls tools[0]. So even a working client would have failed every call.

Both are pinned here against the real bundled servers.
"""

import asyncio

import pytest

from xli.mcp.client import MCPClient, MCPError, server_command
from xli.mcp.registry import SERVERS


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client():
    c = MCPClient()
    yield c
    c.close()


class TestTheClientActuallyCallsServers:
    def test_list_tools_returns_the_servers_real_tools(self, client):
        tools = run(client.list_tools("file_manager"))
        names = [t["name"] for t in tools]
        assert names == ["read_file", "write_file", "list_dir", "grep", "find"]

    def test_call_tool_returns_real_output(self, client, tmp_path):
        (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
        result = run(client.call_tool("file_manager", "read_file", {"path": str(tmp_path / "a.txt")}))
        assert "hello" in str(result)

    def test_an_unknown_tool_raises_rather_than_inventing_success(self, client):
        """The core regression: this used to return a fabricated ok."""
        with pytest.raises(MCPError) as exc:
            run(client.call_tool("file_manager", "no_such_tool", {}))
        assert "Unknown tool" in str(exc.value)

    def test_an_unknown_server_raises(self, client):
        with pytest.raises(MCPError):
            run(client.list_tools("not_a_real_server"))

    def test_one_process_per_server_is_reused(self, client):
        run(client.list_tools("file_manager"))
        first = client._transports["file_manager"].process
        run(client.list_tools("file_manager"))
        assert client._transports["file_manager"].process is first

    def test_close_shuts_every_server_down(self):
        c = MCPClient()
        run(c.list_tools("file_manager"))
        run(c.list_tools("git_mcp"))
        procs = [t.process for t in c._transports.values()]
        c.close()
        assert c._transports == {}
        assert all(p.poll() is not None for p in procs)


class TestServerCommand:
    def test_a_server_is_launched_with_the_running_interpreter(self):
        import sys

        assert server_command("file_manager") == [
            sys.executable,
            "-m",
            "xli.mcp.servers.file_manager",
        ]

    def test_the_package_prefix_is_stable(self):
        assert server_command("git_mcp")[2].startswith("xli.mcp.servers.")


class TestRegistryMatchesReality:
    """Every advertised tool must exist, or MCPBridge calls a ghost."""

    @pytest.mark.parametrize("name", sorted(SERVERS))
    def test_advertised_tools_are_the_ones_the_server_has(self, client, name):
        declared = SERVERS[name]["tools"]
        if not declared:
            pytest.skip(f"{name} ships no server module; nothing to advertise")
        real = [t["name"] for t in run(client.list_tools(name))]
        assert declared == real, f"{name} advertises {declared}, server has {real}"

    def test_no_server_advertises_a_tool_it_lacks(self, client):
        bad = []
        for name, info in SERVERS.items():
            declared = info["tools"]
            if not declared:
                continue
            real = [t["name"] for t in run(client.list_tools(name))]
            for tool in declared:
                if tool not in real:
                    bad.append(f"{name}.{tool}")
        assert not bad, "advertised tools missing from their server: " + ", ".join(bad)

    def test_the_bridge_calls_a_tool_that_exists(self):
        """MCPBridge always picks tools[0]; that one must be real."""
        from xli.mcp.bridge import MCP_ROUTING

        used = {s for routes in MCP_ROUTING.values() for s in routes["pre"] + routes["post"]}
        for name in used:
            tools = SERVERS[name]["tools"]
            assert tools, f"{name} is routed to but advertises no tools"

    def test_lsp_is_recorded_as_having_no_server(self):
        """lsp has no module under xli/mcp/servers; it must not claim tools."""
        assert SERVERS["lsp"]["tools"] == []


class TestEveryServerModuleIsImportable:
    """Two files shipped with hyphens, which Python cannot import at all."""

    def test_no_server_module_has_a_hyphen_in_its_name(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "xli" / "mcp" / "servers"
        bad = [p.name for p in root.glob("*.py") if "-" in p.stem]
        assert not bad, f"not importable as Python modules: {bad}"

    @pytest.mark.parametrize("name", sorted(SERVERS))
    def test_every_server_with_tools_can_be_imported(self, name):
        import importlib

        if not SERVERS[name]["tools"]:
            pytest.skip(f"{name} ships no server module")
        importlib.import_module(f"xli.mcp.servers.{name}")


class TestServersDeclareTheirArguments:
    """Callers cannot guess a tool's parameters, so the server must publish them.

    MCPBridge used to send every tool the same {"query": ..., "code": ...} bag.
    Most tools rejected it with "got an unexpected keyword argument", so the
    pre/post steps silently produced no context at all.
    """

    @pytest.mark.parametrize("name", sorted(SERVERS))
    def test_every_tool_advertises_an_input_schema(self, client, name):
        if not SERVERS[name]["tools"]:
            pytest.skip(f"{name} ships no server module")
        for tool in run(client.list_tools(name)):
            assert "inputSchema" in tool, f"{name}.{tool['name']} declares no schema"
            schema = tool["inputSchema"]
            assert schema["type"] == "object"
            assert "properties" in schema

    @pytest.mark.parametrize("name", sorted(SERVERS))
    def test_declared_properties_match_the_function_signature(self, client, name):
        import importlib
        import inspect

        if not SERVERS[name]["tools"]:
            pytest.skip(f"{name} ships no server module")
        module = importlib.import_module(f"xli.mcp.servers.{name}")
        for tool in run(client.list_tools(name)):
            fn = module.TOOLS[tool["name"]]
            expected = {
                p
                for p, prm in inspect.signature(fn).parameters.items()
                if prm.kind not in (prm.VAR_POSITIONAL, prm.VAR_KEYWORD)
            }
            assert set(tool["inputSchema"]["properties"]) == expected

    def test_required_lists_only_the_parameters_without_defaults(self):
        from xli.mcp.serverkit import input_schema

        def tool(a, b, c=3):
            return (a, b, c)

        schema = input_schema(tool)
        assert schema["required"] == ["a", "b"]

    def test_a_tool_with_all_defaults_has_no_required(self):
        from xli.mcp.serverkit import input_schema

        def tool(a=1):
            return a

        assert "required" not in input_schema(tool)


class TestExtraArgumentsAreTolerated:
    """A caller holding a generic param bag must not break the tool."""

    def test_unknown_keys_are_dropped(self, client):
        """The exact case that broke the bridge: extra keys in the bag."""
        result = run(
            client.call_tool(
                "knowledge", "search_code", {"query": "parser", "code": "ignored"}
            )
        )
        assert "content" in result

    def test_filter_arguments_keeps_only_declared_names(self):
        from xli.mcp.serverkit import filter_arguments

        def tool(a, b=2):
            return (a, b)

        assert filter_arguments(tool, {"a": 1, "z": 9}) == {"a": 1}

    def test_a_kwargs_tool_receives_everything(self):
        from xli.mcp.serverkit import filter_arguments

        def tool(**kwargs):
            return kwargs

        assert filter_arguments(tool, {"a": 1, "z": 9}) == {"a": 1, "z": 9}


class TestTheBridgeProducesContext:
    """The regression: pre/post steps returned an empty string every time."""

    def test_pre_step_returns_something_for_a_routed_agent(self):
        from xli.mcp.bridge import MCPBridge

        bridge = MCPBridge()
        try:
            context = run(bridge.run_mcp_pre_step("coder", "fix the parser bug"))
            assert context, "pre-step produced no context"
            assert "[knowledge]" in context or "[architecture]" in context
        finally:
            bridge.client.close()

    def test_an_agent_with_no_routing_gets_no_context(self):
        from xli.mcp.bridge import MCPBridge

        bridge = MCPBridge()
        try:
            assert run(bridge.run_mcp_pre_step("nobody", "anything")) == ""
        finally:
            bridge.client.close()
