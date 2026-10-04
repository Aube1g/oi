"""`xli mcp serve` — XLI's tools offered to other agents, over MCP.

The protocol details that matter are the boring ones: the handshake happens
before any tool call, a bad tool call comes back as an error *result* rather
than a dead server, and stdout carries frames and nothing else. A stray banner
on stdout corrupts the stream for the whole session, so there is a test that
reads the output of the real CLI process rather than a function.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from xli.mcp.serve import XliMcpServer


class FakeResult:
    def __init__(self, ok=True, data=None, summary="", error=None):
        self._payload = {"ok": ok, "data": data, "summary": summary, "error": error, "tool": "x"}

    def to_dict(self):
        return self._payload


class FakeRegistry:
    """Enough of the real registry for the protocol tests."""

    def __init__(self, results: dict[str, FakeResult] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.results = results or {}

    def names(self):
        return ["read", "chart", "bash"]

    def schema(self):
        return [
            {
                "name": "read",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                "mutates": False,
            },
            {
                "name": "chart",
                "description": "Draw a chart",
                "parameters": {"type": "object", "properties": {"kind": {"type": "string"}}},
                "mutates": False,
            },
            {
                "name": "bash",
                "description": "Run a command",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
                "mutates": True,
            },
        ]

    async def execute(self, name, args=None):
        args = dict(args or {})
        self.calls.append((name, args))
        if name in self.results:
            return self.results[name]
        return FakeResult(ok=True, data={"rows": 1}, summary="готово")


@pytest.fixture
def server() -> XliMcpServer:
    return XliMcpServer(registry=FakeRegistry())


def frames(text: str) -> list[dict]:
    return [json.loads(line) for line in text.strip().splitlines()]


class TestProtocol:
    def test_initialize_before_anything_else(self, server):
        reply = server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05"}}
        )
        assert reply["id"] == 1
        assert reply["result"]["serverInfo"]["name"] == "xli"
        assert reply["result"]["capabilities"]["tools"]["listChanged"] is False
        # The client's protocol version is echoed, which is what strict
        # clients check before they send anything else.
        assert reply["result"]["protocolVersion"] == "2024-11-05"

    def test_ping_answers(self, server):
        assert server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"] == {}

    def test_a_notification_gets_no_reply(self, server):
        assert server.handle_request(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        ) is None

    def test_an_unknown_method_is_an_error_not_a_crash(self, server):
        reply = server.handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/whatever"})
        assert reply["error"]["code"] == -32601


class TestTools:
    def test_the_catalogue_publishes_real_schemas(self, server):
        tools = server.list_tools()
        names = [tool["name"] for tool in tools]
        assert names == ["read", "chart", "bash"]
        read = next(tool for tool in tools if tool["name"] == "read")
        assert read["inputSchema"]["required"] == ["path"]
        assert read["annotations"]["readOnlyHint"] is True
        bash = next(tool for tool in tools if tool["name"] == "bash")
        assert bash["annotations"]["readOnlyHint"] is False

    def test_tools_list_over_the_protocol(self, server):
        reply = server.handle_request({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
        assert len(reply["result"]["tools"]) == 3

    def test_a_rendered_result_arrives_as_the_drawing(self, server):
        """Charts and graphs put the picture in data["text"]; that is what a
        caller wants to see, not a JSON dump of it."""
        registry = FakeRegistry({"chart": FakeResult(data={"text": "▁▂▄▆█", "kind": "spark"})})
        server = XliMcpServer(registry=registry)
        result = server.call("chart", {"kind": "spark"})
        assert result["isError"] is False
        assert result["content"][0]["text"] == "▁▂▄▆█"

    def test_structured_data_is_passed_through(self, server):
        registry = FakeRegistry({"read": FakeResult(data={"path": "a.py", "lines": 3})})
        server = XliMcpServer(registry=registry)
        result = server.call("read", {"path": "a.py"})
        assert result["structuredContent"] == {"path": "a.py", "lines": 3}

    def test_a_failing_tool_is_marked_not_swallowed(self, server):
        registry = FakeRegistry({"bash": FakeResult(ok=False, error="команда упала")})
        server = XliMcpServer(registry=registry)
        result = server.call("bash", {"command": "false"})
        assert result["isError"] is True
        assert "команда упала" in result["content"][0]["text"]

    def test_an_unknown_tool_lists_what_exists(self, server):
        result = server.call("нет-такого", {})
        assert result["isError"] is True
        assert "read" in result["content"][0]["text"]

    def test_a_tool_that_raises_does_not_kill_the_server(self):
        class Exploding(FakeRegistry):
            async def execute(self, name, args=None):
                raise RuntimeError("внутренняя ошибка")

        server = XliMcpServer(registry=Exploding())
        result = server.call("read", {"path": "x"})
        assert result["isError"] is True
        assert "внутренняя ошибка" in result["content"][0]["text"]

    def test_the_allow_list_narrows_the_catalogue(self):
        server = XliMcpServer(registry=FakeRegistry(), tools=["read", "chart"])
        assert [tool["name"] for tool in server.list_tools()] == ["read", "chart"]
        assert server.call("bash", {})["isError"] is True

    def test_the_agent_tool_is_opt_in(self, server):
        assert "xli_agent" not in [tool["name"] for tool in server.list_tools()]
        with_agent = XliMcpServer(registry=FakeRegistry(), with_agent=True)
        assert "xli_agent" in [tool["name"] for tool in with_agent.list_tools()]

    def test_the_agent_tool_requires_a_task(self):
        server = XliMcpServer(registry=FakeRegistry(), with_agent=True)
        result = server.call("xli_agent", {})
        assert result["isError"] is True


class TestLoop:
    def run(self, server, lines: list[str]) -> str:
        out = io.StringIO()
        server.serve(stdin=io.StringIO("\n".join(lines) + "\n"), stdout=out)
        return out.getvalue()

    def test_a_bad_line_is_reported_and_the_loop_continues(self, server):
        text = self.run(
            server,
            ["это не json", json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"})],
        )
        reply = frames(text)
        assert reply[0]["error"]["code"] == -32700
        assert reply[1]["result"] == {}

    def test_every_line_on_stdout_is_a_frame(self, server):
        text = self.run(
            server,
            [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})],
        )
        assert text.endswith("\n")
        for line in text.strip().splitlines():
            assert json.loads(line)["jsonrpc"] == "2.0"


class TestCommandLine:
    """The real process: stdout must be protocol, stderr may say anything."""

    def probe(self, *arguments: str) -> tuple[list[dict], str]:
        frames_in = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        process = subprocess.run(
            [sys.executable, "-m", "xli", "mcp", "serve", *arguments],
            input="\n".join(json.dumps(frame) for frame in frames_in) + "\n",
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert process.returncode == 0, process.stderr
        return frames(process.stdout), process.stderr

    def test_the_cli_serves_twenty_three_tools(self):
        replies, stderr = self.probe()
        assert replies[0]["result"]["serverInfo"]["name"] == "xli"
        tools = replies[1]["result"]["tools"]
        assert len(tools) == 23
        assert "chart" in [tool["name"] for tool in tools]
        # Whatever the CLI said goes to stderr, where it cannot corrupt a frame.
        assert "инструментов" in stderr

    def test_the_allow_list_reaches_the_cli(self):
        replies, _ = self.probe("--tools", "read,chart")
        assert sorted(tool["name"] for tool in replies[1]["result"]["tools"]) == ["chart", "read"]
