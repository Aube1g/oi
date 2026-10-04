#!/usr/bin/env python3
"""
XLI Kernel transports — stdio and unix-socket pumps for KernelServer.

Both pumps are thin: read a line, hand it to the server, write back whatever
frames the server produced plus anything it queued as notifications. Keeping the
transport this dumb means a Go frontend can speak to `xli serve --stdio`
exactly as it would to a socket, and both paths are covered by the same tests.

Stdout carries protocol frames only. Logs go to stderr, so a client parsing
stdout never trips over a stray warning.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from xli.kernel.protocol import encode_line
from xli.kernel.server import KernelServer
from xli.paths import xli_path

#: XLI_RUNTIME_DIR wins, because a socket is a runtime artefact and it is
#: reasonable to want it on a tmpfs. The fallback still goes through xli_home()
#: so that relocating xli's state relocates the socket too.
DEFAULT_SOCKET_DIR = Path(os.environ.get("XLI_RUNTIME_DIR") or xli_path("run"))


def socket_path(name: str = "kernel", runtime_dir: Path | None = None) -> Path:
    base = Path(runtime_dir) if runtime_dir else DEFAULT_SOCKET_DIR
    return base / f"{name}.sock"


class LineWriter:
    """Serialises frame writes so concurrent notify()/reply() cannot interleave."""

    def __init__(self, write):
        self._write = write
        self._lock = asyncio.Lock()

    async def write_frames(self, frames) -> None:
        if not frames:
            return
        async with self._lock:
            payload = b"".join(encode_line(f) for f in frames)
            await self._write(payload)


async def serve_connection(server: KernelServer, reader, writer) -> None:
    """Drive one bidirectional ndjson connection to EOF."""
    out = LineWriter(writer)
    try:
        while True:
            line = await reader()
            if not line:
                break
            if not line.strip():
                continue
            replies = await server.feed_line(line)
            await out.write_frames(replies)
            await out.write_frames(await server.drain())
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    finally:
        # Flush anything queued by a streaming handler before hanging up.
        await out.write_frames(await server.drain())


async def serve_stdio(server: KernelServer) -> None:
    """Serve the kernel over stdin/stdout using a reader thread for stdin."""
    loop = asyncio.get_running_loop()
    stdin = sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin
    stdout = sys.stdout.buffer if hasattr(sys.stdout, "buffer") else sys.stdout

    async def read_line() -> bytes:
        return await loop.run_in_executor(None, stdin.readline)

    async def write(payload: bytes) -> None:
        await loop.run_in_executor(None, _blocking_write, stdout, payload)

    await serve_connection(server, read_line, write)


def _blocking_write(stream, payload: bytes) -> None:
    stream.write(payload)
    stream.flush()


async def serve_unix(
    server: KernelServer,
    path: Path | None = None,
    *,
    stop_event: asyncio.Event | None = None,
) -> Path:
    """Serve the kernel on a unix socket until `stop_event` is set."""
    sock_path = Path(path) if path else socket_path()
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        # A stale socket left by a crashed kernel makes bind() fail forever.
        try:
            sock_path.unlink()
        except OSError:
            pass

    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def read_line() -> bytes:
            return await reader.readline()

        async def write(payload: bytes) -> None:
            writer.write(payload)
            await writer.drain()

        try:
            await serve_connection(server, read_line, write)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - best effort teardown
                pass

    srv = await asyncio.start_unix_server(on_client, path=str(sock_path))
    try:
        os.chmod(sock_path, 0o600)
    except OSError:
        pass

    stop = stop_event or asyncio.Event()
    serve_task = asyncio.create_task(srv.serve_forever(), name="xli-kernel-unix")
    try:
        await stop.wait()
    finally:
        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass
        srv.close()
        await srv.wait_closed()
        try:
            sock_path.unlink(missing_ok=True)
        except OSError:
            pass

    return sock_path


async def serve_http(
    server: KernelServer,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    stop_event: asyncio.Event | None = None,
    web_root: Path | None = None,
) -> None:
    """Serve the kernel over HTTP/SSE (pure asyncio, zero dependencies).

    With `web_root` set, the same port also serves the web frontend — the
    kernel and the page that talks to it come from one process, which is why
    the browser never has to know where the protocol lives.
    """
    from xli.kernel.http_transport import serve_http_with_web

    await serve_http_with_web(
        server, host, port, web_root=web_root, stop_event=stop_event, announce=True
    )


async def unix_client(path: Path):
    """Open a client connection to a running kernel; returns (reader, writer)."""
    return await asyncio.open_unix_connection(path=str(path))
