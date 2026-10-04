#!/usr/bin/env python3
"""HTTP + SSE transport for the kernel, and the host for the web frontend.

`xli serve --http` has named this module since the flag was added and the
module did not exist, so the flag raised ImportError every time it was used.
This is it, written to the same rules as the stdio and unix pumps: the
transport is dumb, the kernel stays transport-agnostic, and nothing here needs
a dependency (it is `asyncio` and the standard library).

Three endpoints:

    POST /rpc        one JSON-RPC frame (or a list of them) in, replies out
    GET  /events     Server-Sent Events: notifications as they happen
    GET  /*          the web frontend, if a web root was given

The split matters. A long `agent.run` occupies its POST until it finishes; the
notifications it produces — tool calls, assistant text, step counters — arrive
on `/events` in parallel. That is exactly how the TUI works in-process and how
the Neovim plugin works over the socket, so the web UI is a client like any
other rather than a special case.

The notification pump is the one subtle part. The kernel has a single outbox
queue, which is right for a 1:1 connection and wrong for HTTP, where several
browsers may be watching. So one task drains the outbox and fans the frames out
to every subscriber; a slow browser cannot stall the agent, because each
subscriber has its own bounded queue and the oldest frames are dropped rather
than the newest.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from xli.kernel.server import KernelServer

#: How long a subscriber's queue can get before we start dropping. A browser tab
#: that has been asleep for an hour should come back to the *recent* state, not
#: to a backlog of every frame it missed.
SUBSCRIBER_BACKLOG = 512

#: SSE comment frames, so idle connections do not look dead to a proxy.
KEEPALIVE_SECONDS = 15.0

CORS_HEADERS = (
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
    ("Access-Control-Allow-Headers", "Content-Type, Authorization"),
    ("Access-Control-Max-Age", "86400"),
)


class HttpTransport:
    """Serves one running kernel over HTTP."""

    def __init__(self, kernel: KernelServer, *, web_root: Path | None = None) -> None:
        self.kernel = kernel
        self.web_root = Path(web_root) if web_root else None
        self._subscribers: set[asyncio.Queue] = set()
        self._pump: asyncio.Task | None = None

    # ------------------------------------------------------------------ pump
    async def start(self) -> None:
        """Start fanning kernel notifications out to subscribers."""
        if self._pump is None:
            self._pump = asyncio.create_task(self._fan_out(), name="xli-http-notify-pump")

    async def stop(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            try:
                await self._pump
            except asyncio.CancelledError:
                pass
            self._pump = None

    async def _fan_out(self) -> None:
        while True:
            frames = await self.kernel.drain(timeout=0.25)
            if not frames:
                continue
            for frame in frames:
                payload = frame.to_dict() if hasattr(frame, "to_dict") else frame
                for queue in list(self._subscribers):
                    if queue.full():
                        # Drop the oldest: the recent state is the useful one.
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        queue.put_nowait(payload)
                    except asyncio.QueueFull:
                        pass

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_BACKLOG)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    # ----------------------------------------------------------------- routes
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One keep-alive HTTP connection."""
        await self.start()
        try:
            while True:
                request = await self._read_request(reader)
                if request is None:
                    break
                method, path, headers, body = request
                keep_alive = _wants_keep_alive(headers)

                if method == "OPTIONS":
                    await self._send(writer, 204, b"", "text/plain", extra=CORS_HEADERS)
                elif path == "/rpc" or (path == "/" and method == "POST"):
                    await self._handle_rpc(writer, body)
                elif path == "/events":
                    # Takes the connection over; returns when the client leaves.
                    await self._handle_events(writer)
                    return
                elif path == "/health":
                    await self._send_json(writer, 200, {
                        "ok": True,
                        "kernel": self.kernel.name,
                        "methods": len(self.kernel.list_methods()),
                        "subscribers": len(self._subscribers),
                        "web": bool(self.web_root),
                    })
                elif method == "GET":
                    await self._handle_static(writer, path)
                else:
                    await self._send_json(writer, 405, {"error": "method not allowed"})

                if not keep_alive:
                    break
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        finally:
            with _suppress_all():
                writer.close()
                await writer.wait_closed()

    # ---------------------------------------------------------------- request
    async def _read_request(self, reader: asyncio.StreamReader):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionResetError):
            return None

        lines = head.decode("latin-1").split("\r\n")
        if not lines or not lines[0]:
            return None
        parts = lines[0].split(" ")
        if len(parts) < 2:
            return None
        method, target = parts[0].upper(), parts[1]
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()

        body = b""
        length = int(headers.get("content-length") or 0)
        if length:
            body = await reader.readexactly(length)
        return method, urlparse(target).path or "/", headers, body

    async def _handle_rpc(self, writer, body: bytes) -> None:
        try:
            payload = json.loads(body.decode("utf-8") or "null")
        except (ValueError, UnicodeDecodeError) as exc:
            await self._send_json(writer, 400, {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": f"parse error: {exc}"},
            })
            return

        frames = payload if isinstance(payload, list) else [payload]
        replies: list[dict[str, Any]] = []
        for frame in frames:
            if not isinstance(frame, dict):
                replies.append({"jsonrpc": "2.0", "id": None,
                                "error": {"code": -32600, "message": "invalid request"}})
                continue
            try:
                # feed_line takes the raw text and decodes it, which is the
                # entry point every transport uses; feed() wants a Frame object.
                produced = await self.kernel.feed_line(json.dumps(frame, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001 - one bad call must not kill the server
                replies.append({"jsonrpc": "2.0", "id": frame.get("id"),
                                "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}})
                continue
            replies.extend(
                reply.to_dict() if hasattr(reply, "to_dict") else reply for reply in produced
            )

        # Drain what the calls produced so far so a short call's notifications
        # arrive even if nobody is listening on /events.
        if not self._subscribers:
            for frame in await self.kernel.drain():
                payload_frame = frame.to_dict() if hasattr(frame, "to_dict") else frame
                replies.append(payload_frame)

        # A single request normally gets a single reply object. It gets a list
        # when it also produced notifications that nobody else is listening for
        # — dropping them would lose the tool calls from a short run.
        if isinstance(payload, list) or len(replies) != 1:
            answer: Any = replies
        else:
            answer = replies[0] if replies else None
        body_out = json.dumps(answer, ensure_ascii=False).encode("utf-8")
        await self._send(writer, 200, body_out, "application/json; charset=utf-8", extra=CORS_HEADERS)

    async def _handle_events(self, writer) -> None:
        """Server-Sent Events: notifications until the client goes away."""
        queue = self.subscribe()
        headers = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream; charset=utf-8\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Connection: keep-alive\r\n"
            b"X-Accel-Buffering: no\r\n"
            + b"".join(f"{name}: {value}\r\n".encode() for name, value in CORS_HEADERS)
            + b"\r\n"
        )
        writer.write(headers)
        await writer.drain()
        writer.write(b": connected\n\n")
        await writer.drain()

        try:
            while True:
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    writer.write(b": keepalive\n\n")
                    await writer.drain()
                    continue
                data = json.dumps(frame, ensure_ascii=False)
                writer.write(f"data: {data}\n\n".encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            self.unsubscribe(queue)

    async def _handle_static(self, writer, path: str) -> None:
        if self.web_root is None:
            await self._send_json(writer, 404, {"error": "no web root configured"})
            return

        relative = unquote(path).lstrip("/") or "index.html"
        candidate = (self.web_root / relative).resolve()
        root = self.web_root.resolve()
        # Path traversal: anything that escapes the root is a 404, not a file.
        inside = str(candidate).startswith(str(root))
        if not inside or not candidate.is_file():
            # A route (no file extension) falls back to the page itself; a
            # missing asset gets a 404, because a stylesheet served as HTML is
            # a worse failure than an honest miss.
            looks_like_route = candidate.suffix == ""
            fallback = root / "index.html" if looks_like_route and (root / "index.html").is_file() else None
            if fallback is None:
                await self._send_json(writer, 404, {"error": "not found", "path": path})
                return
            candidate = fallback

        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        body = candidate.read_bytes()
        await self._send(writer, 200, body, content_type, extra=CORS_HEADERS)

    # ----------------------------------------------------------------- output
    async def _send_json(self, writer, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        await self._send(writer, status, body, "application/json; charset=utf-8", extra=CORS_HEADERS)

    async def _send(self, writer, status: int, body: bytes, content_type: str, *,
                    extra: tuple[tuple[str, str], ...] = ()) -> None:
        reason = {200: "OK", 204: "No Content", 400: "Bad Request", 404: "Not Found",
                  405: "Method Not Allowed"}.get(status, "OK")
        head = [f"HTTP/1.1 {status} {reason}", f"Content-Type: {content_type}",
                f"Content-Length: {len(body)}", "Connection: keep-alive"]
        head.extend(f"{name}: {value}" for name, value in extra)
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)
        await writer.drain()


def _wants_keep_alive(headers: dict[str, str]) -> bool:
    connection = (headers.get("connection") or "").lower()
    if connection == "close":
        return False
    return True


class _suppress_all:
    """`contextlib.suppress(Exception)`, for the cleanup path."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None


async def serve_http_with_web(
    kernel: KernelServer,
    host: str,
    port: int,
    *,
    web_root: Path | None = None,
    stop_event: asyncio.Event | None = None,
    announce: bool = True,
) -> None:
    """Run the HTTP transport until `stop_event` (or forever)."""
    transport = HttpTransport(kernel, web_root=web_root)
    await transport.start()
    server = await asyncio.start_server(transport.handle, host, port)
    if announce:
        where = f"http://{host}:{port}"
        print(f"  веб-интерфейс XLI: {where}" if web_root else f"  ядро слушает {where}",
              file=sys.stderr)

    stop = stop_event or asyncio.Event()
    serving = asyncio.create_task(server.serve_forever(), name="xli-http")
    try:
        await stop.wait()
    finally:
        serving.cancel()
        with _suppress_all():
            await serving
        server.close()
        with _suppress_all():
            await server.wait_closed()
        await transport.stop()
