#!/usr/bin/env python3
"""A minimal MCP server over stdio, for testing the client against a process.

It is deliberately annoying in the ways real servers are: it prints a banner on
stdout that is not JSON, logs to stderr, sends a notification before the reply,
and answers `tools/call` with the content-block shape the protocol specifies.
A client that reads "the first line" or "the first JSON reply" fails here.
"""

import json
import sys

BANNER = "fake-mcp-server v1 — starting"


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> int:
    print(BANNER)
    sys.stderr.write("fake server: ready\n")
    sys.stderr.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = message.get("method")
        message_id = message.get("id")

        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "fake-server", "version": "1.0"},
                    },
                }
            )
        elif method == "notifications/initialized":
            # A notification: no reply, by protocol.
            continue
        elif method == "tools/list":
            # A notification before the reply — the client must skip it.
            send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"note": "listing"}})
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "tools": [
                            {
                                "name": "echo",
                                "description": "Echo the text back",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"text": {"type": "string"}},
                                    "required": ["text"],
                                },
                            },
                            {"name": "boom", "description": "Always fails"},
                        ]
                    },
                }
            )
        elif method == "tools/call":
            params = message.get("params") or {}
            if params.get("name") == "boom":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": message_id,
                        "error": {"code": -32000, "message": "boom, as requested"},
                    }
                )
            else:
                text = (params.get("arguments") or {}).get("text", "")
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": message_id,
                        "result": {
                            "content": [{"type": "text", "text": f"echo: {text}"}],
                            "isError": False,
                        },
                    }
                )
        elif message_id is not None:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "error": {"code": -32601, "message": f"unknown method {method}"},
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
