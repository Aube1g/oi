#!/usr/bin/env python3
"""
XLI MCP Transport v4 — StdIO / SSE / WebSocket
"""

import subprocess
import json
from abc import ABC, abstractmethod

from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.mcp.transport")


class Transport(ABC):
    """Base transport"""

    @abstractmethod
    def send(self, message: dict) -> str:
        """Send message and get response"""
        pass

    @abstractmethod
    def close(self):
        """Close transport"""
        pass


class StdioTransport(Transport):
    """Stdio-based transport"""

    def __init__(self, command: list):
        self.command = command
        self.process: subprocess.Popen | None = None
        self._start()

    def _start(self):
        """Start subprocess"""
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        logger.log_structured("DEBUG", "mcp.transport",
                             f"Started: {' '.join(self.command)}")

    def send(self, message: dict) -> str:
        """Send JSON-RPC message"""
        if not self.process or self.process.poll() is not None:
            self._start()

        data = json.dumps(message) + "\n"
        self.process.stdin.write(data)
        self.process.stdin.flush()

        # Read response
        response = self.process.stdout.readline()
        return response

    def close(self):
        """Terminate subprocess"""
        if self.process:
            self.process.terminate()
            self.process.wait()
            logger.log_structured("DEBUG", "mcp.transport", "Closed stdio")


class SSETransport(Transport):
    """Server-Sent Events transport (placeholder)"""

    def __init__(self, url: str):
        self.url = url

    def send(self, message: dict) -> str:
        logger.log_structured("WARN", "mcp.transport", "SSE not implemented")
        return '{"error": "SSE not implemented"}'

    def close(self):
        pass


class WebSocketTransport(Transport):
    """WebSocket transport (placeholder)"""

    def __init__(self, url: str):
        self.url = url

    def send(self, message: dict) -> str:
        logger.log_structured("WARN", "mcp.transport", "WebSocket not implemented")
        return '{"error": "WebSocket not implemented"}'

    def close(self):
        pass


def create_transport(transport_type: str, config: dict) -> Transport:
    """Create transport instance"""
    if transport_type == "stdio":
        return StdioTransport(config["command"])
    elif transport_type == "sse":
        return SSETransport(config["url"])
    elif transport_type == "websocket":
        return WebSocketTransport(config["url"])
    else:
        raise ValueError(f"Unknown transport: {transport_type}")

