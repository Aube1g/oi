#!/usr/bin/env python3
"""
XLI MCP Transport — stdio, and the HTTP endpoint the remote servers use.

Two things here were wrong in a way that only shows up against a real server:

  * **One line is not one message.** Servers log to stderr (fine) but some
    also print banners to stdout, and many send `notifications/...` or replies
    to other request ids before the reply we are waiting for. The old reader
    returned the first line it saw, so a startup banner became "malformed
    response" and the call failed. The reader now skips anything that is not
    JSON with the id it asked for.

  * **A blocked pipe is a hang.** `stderr` was a PIPE nobody read; a server
    that writes more than the pipe buffer to stderr deadlocks — the classic
    way to make an MCP call hang forever. Stderr is drained by a daemon thread
    and kept for diagnostics.

The handshake is here too: the protocol requires `initialize` followed by an
`initialized` notification before any tool call, and a server that never
receives them may answer with an error or not at all.
"""

import json
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod

from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.mcp.transport")

#: The protocol revision XLI speaks. Servers negotiate down to a version they
#: support; anything they answer with is accepted.
PROTOCOL_VERSION = "2025-06-18"


class TransportError(RuntimeError):
    """The transport could not deliver or collect a message."""


class Transport(ABC):
    """Base transport"""

    @abstractmethod
    def send(self, message: dict, *, timeout: float | None = None) -> dict:
        """Send a JSON-RPC message and return the matching reply."""

    @abstractmethod
    def close(self):
        """Close transport"""


class StdioTransport(Transport):
    """A server run as a child process, spoken to in newline-delimited JSON."""

    def __init__(
        self,
        command: list,
        *,
        env: dict | None = None,
        timeout: float = 60.0,
        startup_timeout: float = 20.0,
    ):
        self.command = [str(part) for part in command]
        self.env = env or {}
        self.timeout = timeout
        self.startup_timeout = startup_timeout
        self.process: subprocess.Popen | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: list[str] = []
        self._lock = threading.Lock()
        self._initialized = False
        self._next_id = 0

    # ------------------------------------------------------------- process
    def _start(self):
        """Start the subprocess and the two reader threads."""
        environment = dict(os.environ)
        environment.update({str(k): str(v) for k, v in self.env.items()})
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=environment,
            )
        except FileNotFoundError as exc:
            raise TransportError(
                f"command not found: {self.command[0]!r} — is it installed and on PATH?"
            ) from exc
        except OSError as exc:
            raise TransportError(f"could not start {' '.join(self.command)}: {exc}") from exc

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        logger.log_structured("DEBUG", "mcp.transport", f"Started: {' '.join(self.command)}")

    def _read_stdout(self):
        process = self.process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            self._lines.put(line)
        self._lines.put(None)  # EOF: wake every waiter

    def _read_stderr(self):
        """Drain stderr so the child can never block writing to it."""
        process = self.process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            text = line.rstrip("\n")
            if text:
                self._stderr.append(text)
                del self._stderr[:-40]  # keep the tail, not the history

    @property
    def stderr_tail(self) -> str:
        """What the server said on stderr — the only diagnostics some give."""
        return "\n".join(self._stderr[-8:])

    def _write(self, message: dict) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise TransportError("server is not running")
        if process.poll() is not None:
            raise TransportError(
                f"server exited with code {process.returncode}: {self.stderr_tail or 'no output'}"
            )
        try:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise TransportError(f"server closed its input: {exc}") from exc

    # ----------------------------------------------------------------- rpc
    def send(self, message: dict, *, timeout: float | None = None) -> dict:
        with self._lock:
            if self.process is None or self.process.poll() is not None:
                self._start()
            if not self._initialized:
                self._handshake()

            wanted = message.get("id")
            self._write(message)

            deadline = time.monotonic() + (timeout or self.timeout)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TransportError(
                        f"no reply to {message.get('method')} within "
                        f"{timeout or self.timeout:.0f}s"
                        + (f"; server said: {self.stderr_tail}" if self.stderr_tail else "")
                    )
                try:
                    line = self._lines.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    continue
                if line is None:
                    raise TransportError(
                        f"server exited with code "
                        f"{self.process.returncode if self.process else '?'}"
                        + (f": {self.stderr_tail}" if self.stderr_tail else "")
                    )
                reply = self._parse(line)
                if reply is None:
                    continue
                # Notifications and replies to earlier requests are skipped;
                # a server is allowed to send them at any time.
                if wanted is None or reply.get("id") == wanted:
                    return reply
                logger.log_structured(
                    "DEBUG", "mcp.transport", f"skipped reply id={reply.get('id')} while waiting for {wanted}"
                )

    @staticmethod
    def _parse(line: str) -> dict | None:
        text = line.strip()
        if not text:
            return None
        # Some servers wrap each message in an SSE `data:` field even on stdio.
        if text.startswith("data:"):
            text = text[5:].strip()
        if not text.startswith("{"):
            return None
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def _handshake(self) -> None:
        """initialize + notifications/initialized, once per process."""
        self._initialized = True  # set first: the reply is read by this call
        self._write(
            {
                "jsonrpc": "2.0",
                "id": "_init",
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"roots": {"listChanged": False}, "sampling": {}},
                    "clientInfo": {"name": "xli", "version": _version()},
                },
            }
        )
        reply = self._await("_init", self.startup_timeout, "initialize")
        result = reply.get("result")
        if isinstance(result, dict):
            logger.log_structured(
                "DEBUG",
                "mcp.transport",
                f"initialized with {result.get('serverInfo', {}).get('name', 'unknown server')}",
            )
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def _await(self, wanted: str, timeout: float, method: str) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportError(
                    f"no reply to {method} within {timeout:.0f}s"
                    + (f"; server said: {self.stderr_tail}" if self.stderr_tail else "")
                )
            try:
                line = self._lines.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if line is None:
                raise TransportError(
                    f"server exited during {method}"
                    + (f": {self.stderr_tail}" if self.stderr_tail else "")
                )
            reply = self._parse(line)
            if reply is not None and reply.get("id") == wanted:
                return reply

    def close(self):
        """Terminate subprocess"""
        process, self.process = self.process, None
        self._initialized = False
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        logger.log_structured("DEBUG", "mcp.transport", "Closed stdio")


class HTTPTransport(Transport):
    """JSON-RPC over HTTP — how the hosted MCP servers are reached.

    Streamable HTTP replies either with a JSON body or with an SSE stream; both
    are read here. `Authorization` is taken from the server's `env` (the
    `NOTION_TOKEN`-style variable other clients use) or from a `headers` entry
    in the config, so a token does not have to be pasted into a URL.
    """

    def __init__(
        self,
        url: str,
        *,
        env: dict | None = None,
        headers: dict | None = None,
        timeout: float = 60.0,
        startup_timeout: float = 20.0,
    ):
        self.url = url
        self.env = env or {}
        self.headers = headers or {}
        self.timeout = timeout
        self.startup_timeout = startup_timeout
        self._session = ""
        self._initialized = False

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": f"xli/{_version()}",
            **self.headers,
        }
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        for key, value in self.env.items():
            if key.lower() in ("authorization", "token", "api_key", "apikey"):
                headers.setdefault(
                    "Authorization",
                    value if str(value).lower().startswith("bearer") else f"Bearer {value}",
                )
        return headers

    def send(self, message: dict, *, timeout: float | None = None) -> dict:
        if not self._initialized:
            self._initialized = True
            self._rpc(
                {
                    "jsonrpc": "2.0",
                    "id": "_init",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "xli", "version": _version()},
                    },
                },
                timeout=self.startup_timeout,
            )
            self._notify({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        return self._rpc(message, timeout=timeout)

    def _rpc(self, message: dict, *, timeout: float | None = None) -> dict:
        body = json.dumps(message).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=body, headers=self._headers(), method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                session = response.headers.get("Mcp-Session-Id") or response.headers.get(
                    "mcp-session-id"
                )
                if session:
                    self._session = session
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
            except Exception:  # noqa: BLE001 - the body is optional
                pass
            raise TransportError(f"HTTP {exc.code} from {self.url}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise TransportError(f"could not reach {self.url}: {exc.reason}") from exc

        for reply in _iter_replies(raw):
            if message.get("id") is None or reply.get("id") == message.get("id"):
                return reply
        raise TransportError(f"no JSON-RPC reply from {self.url}")

    def _notify(self, message: dict) -> None:
        try:
            request = urllib.request.Request(
                self.url,
                data=json.dumps(message).encode("utf-8"),
                headers=self._headers(),
                method="POST",
            )
            urllib.request.urlopen(request, timeout=self.timeout).close()
        except Exception as exc:  # noqa: BLE001 - a notification's failure is not fatal
            logger.log_structured("DEBUG", "mcp.transport", f"notification failed: {exc}")

    def close(self):
        self._session = ""
        self._initialized = False


def _iter_replies(raw: str) -> list[dict]:
    """Every JSON-RPC object in a response body, SSE or plain JSON."""
    text = raw.strip()
    if not text:
        return []
    if text.startswith("{"):
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return []
        return [value] if isinstance(value, dict) else []
    out: list[dict] = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            value = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def _version() -> str:
    try:
        from xli.version import VERSION

        return str(VERSION)
    except Exception:  # noqa: BLE001 - the user agent is not worth a failure
        return "0"


class SSETransport(Transport):
    """Kept for the old server-configured `sse` endpoints: HTTP transport is a
    superset of what those endpoints do, and this class forwards to it."""

    def __init__(self, url: str, **kwargs):
        self._http = HTTPTransport(url, **kwargs)

    def send(self, message: dict, *, timeout: float | None = None) -> dict:
        return self._http.send(message, timeout=timeout)

    def close(self):
        self._http.close()


class WebSocketTransport(Transport):
    """WebSocket MCP endpoints are not supported; failing loudly beats hanging."""

    def __init__(self, url: str, **kwargs):
        self.url = url

    def send(self, message: dict, *, timeout: float | None = None) -> dict:
        raise TransportError(
            f"{self.url}: WebSocket MCP servers are not supported yet — "
            "use the server's HTTP endpoint or run it over stdio"
        )

    def close(self):
        return None
