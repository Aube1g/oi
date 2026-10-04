"""`xli serve --http` and `xli web`: the HTTP/SSE transport.

A real server on an ephemeral port, spoken to with raw HTTP over asyncio
streams. That is the only way to check the parts that matter and cannot be
checked by calling a function: keep-alive, Server-Sent Events, CORS preflight,
and the fact that a queued notification reaches a browser instead of vanishing
into the kernel's outbox.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from urllib.parse import quote

import pytest

from xli.kernel.http_transport import HttpTransport
from xli.kernel.server import KernelServer

WEB_ROOT = Path(__file__).resolve().parents[1] / "xli" / "web"


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def json(self):
        return json.loads(self.body.decode("utf-8"))


class Harness:
    """A running transport, plus the raw-HTTP helpers to talk to it."""

    def __init__(self, server, port: int) -> None:
        self.server = server
        self.port = port
        self.transport: HttpTransport | None = None

    async def request(self, method: str, path: str, body: bytes | str = b"",
                      headers: dict[str, str] | None = None) -> Response:
        return await self._one(method, path, body, headers)

    async def _connection(self):
        return await asyncio.open_connection("127.0.0.1", self.port)

    async def _one(self, method, path, body, headers):
        reader, writer = await self._connection()
        try:
            response = await self._exchange(reader, writer, method, path, body, headers)
        finally:
            writer.close()
        return response

    async def _exchange(self, reader, writer, method, path, body=b"", headers=None, *,
                        close=True) -> Response:
        payload = body.encode("utf-8") if isinstance(body, str) else body
        # A browser percent-encodes a Cyrillic path; the test must too.
        target = quote(path, safe="/%?=&#")
        head = [f"{method} {target} HTTP/1.1", "Host: localhost",
                f"Content-Length: {len(payload)}",
                "Connection: close" if close else "Connection: keep-alive"]
        for name, value in (headers or {}).items():
            head.append(f"{name}: {value}")
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + payload)
        await writer.drain()
        return await read_response(reader)


async def read_response(reader) -> Response:
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
    length = int(headers.get("content-length") or 0)
    body = await reader.readexactly(length) if length else b""
    return Response(status, headers, body)


class BlockingTransport(HttpTransport):
    """Records which kernel frames were broadcast, for the SSE tests."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.broadcast: list[dict] = []

    async def _fan_out(self) -> None:
        while True:
            for frame in await self.kernel.drain(timeout=0.2):
                payload = frame.to_dict() if hasattr(frame, "to_dict") else frame
                self.broadcast.append(payload)
                for queue in list(self._subscribers):
                    queue.put_nowait(payload)


#: The fixture owns one event loop for the whole test: a server bound to a
#: closed loop would accept the connection and then never answer.
_LOOP: asyncio.AbstractEventLoop | None = None


def run(coro):
    assert _LOOP is not None, "the harness fixture must be active"
    return _LOOP.run_until_complete(coro)


@pytest.fixture
def harness() -> Harness:
    """A kernel with two test methods, served on a random port."""
    global _LOOP

    async def build() -> Harness:
        kernel = KernelServer()
        kernel.register("test.echo", lambda value="": {"value": value},
                        doc="Echo a value back.", required=("value",))

        async def noisy(value: str = "x") -> dict:
            await kernel.notify("test.noise", {"value": value})
            return {"notified": True}

        kernel.register("test.noise", noisy, doc="Answer and also notify.")

        transport = BlockingTransport(kernel, web_root=WEB_ROOT)
        await transport.start()
        server = await asyncio.start_server(transport.handle, "127.0.0.1", 0)
        harness = Harness(server, port=server.sockets[0].getsockname()[1])
        harness.transport = transport
        return harness

    loop = asyncio.new_event_loop()
    _LOOP = loop
    built = loop.run_until_complete(build())
    try:
        yield built
    finally:
        loop.run_until_complete(built.transport.stop())
        built.server.close()
        loop.run_until_complete(built.server.wait_closed())
        # A test may leave a connection handler parked on the SSE queue; cancel
        # it here, or it complains about a closed loop during teardown.
        for task in asyncio.all_tasks(loop):
            task.cancel()
        with contextlib.suppress(Exception):
            loop.run_until_complete(asyncio.gather(*asyncio.all_tasks(loop),
                                                   return_exceptions=True))
        loop.close()
        _LOOP = None


class TestRpc:
    def test_a_request_gets_a_reply(self, harness):
        response = run(harness.request(
            "POST", "/rpc",
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "test.echo",
                        "params": {"value": "привет"}}),
            {"Content-Type": "application/json"},
        ))
        assert response.status == 200
        assert response.headers["content-type"].startswith("application/json")
        assert response.json["id"] == 1
        assert response.json["result"] == {"value": "привет"}

    def test_cors_is_open_so_another_page_can_talk_to_the_kernel(self, harness):
        response = run(harness.request("OPTIONS", "/rpc"))
        assert response.status == 204
        assert response.headers["access-control-allow-origin"] == "*"
        assert "POST" in response.headers["access-control-allow-methods"]

    def test_bad_json_is_reported_not_fatal(self, harness):
        response = run(harness.request("POST", "/rpc", "{не json",
                                       {"Content-Type": "application/json"}))
        assert response.status == 400
        assert response.json["error"]["code"] == -32700

    def test_an_unknown_method_comes_back_as_a_json_rpc_error(self, harness):
        response = run(harness.request(
            "POST", "/rpc", json.dumps({"jsonrpc": "2.0", "id": 7, "method": "нет.такого"}),
        ))
        assert response.json["id"] == 7
        assert response.json["error"]["code"] == -32601

    def test_a_batch_of_requests_is_answered_in_order(self, harness):
        frames = [
            {"jsonrpc": "2.0", "id": 1, "method": "test.echo", "params": {"value": "а"}},
            {"jsonrpc": "2.0", "id": 2, "method": "test.echo", "params": {"value": "б"}},
        ]
        response = run(harness.request("POST", "/rpc", json.dumps(frames)))
        assert [reply["id"] for reply in response.json] == [1, 2]
        assert [reply["result"]["value"] for reply in response.json] == ["а", "б"]

    def test_two_requests_share_one_connection(self, harness):
        async def scenario():
            reader, writer = await asyncio.open_connection("127.0.0.1", harness.port)
            try:
                first = await harness._exchange(
                    reader, writer, "POST", "/rpc",
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "kernel.ping"}),
                    close=False,
                )
                second = await harness._exchange(
                    reader, writer, "POST", "/rpc",
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "kernel.ping"}),
                    close=False,
                )
                return first, second
            finally:
                writer.close()

        first, second = run(scenario())
        assert first.json["id"] == 1 and second.json["id"] == 2

    def test_a_notification_without_listeners_rides_along_with_the_reply(self, harness):
        """A short run's tool calls must not be lost just because no page is
        listening on /events."""
        response = run(harness.request(
            "POST", "/rpc", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "test.noise",
                                        "params": {"value": "один"}}),
        ))
        payload = response.json
        frames = payload if isinstance(payload, list) else [payload]
        methods = [frame.get("method") for frame in frames]
        assert "test.noise" in methods
        assert any(frame.get("id") == 1 for frame in frames)


class TestEvents:
    def test_a_subscriber_receives_the_kernel_notifications(self, harness):
        async def scenario():
            reader, writer = await asyncio.open_connection("127.0.0.1", harness.port)
            writer.write(b"GET /events HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()

            status = await asyncio.wait_for(reader.readline(), timeout=5)
            assert b"200" in status
            headers = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if line in (b"\r\n", b"\n"):
                    break
                name, _, value = line.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()
            assert headers["content-type"].startswith("text/event-stream")
            assert headers["access-control-allow-origin"] == "*"

            # The stream opens with a comment; frames arrive as they happen.
            connected = await asyncio.wait_for(reader.readline(), timeout=5)
            assert connected.startswith(b":")

            # Fire the notification on a second connection.
            second_reader, second = await asyncio.open_connection("127.0.0.1", harness.port)
            await harness._exchange(
                second_reader, second, "POST", "/rpc",
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "test.noise",
                            "params": {"value": "событие"}}),
            )
            second.close()

            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if line.startswith(b"data: "):
                    writer.close()
                    return json.loads(line[6:].decode("utf-8"))
                if not line:
                    raise AssertionError("поток событий закрылся")

        frame = run(scenario())
        assert frame["method"] == "test.noise"
        assert frame["params"]["value"] == "событие"

    def test_the_pump_broadcasts_instead_of_holding_frames_back(self, harness):
        async def scenario():
            queue = harness.transport.subscribe()
            reader, writer = await asyncio.open_connection("127.0.0.1", harness.port)
            try:
                await harness._exchange(
                    reader, writer, "POST", "/rpc",
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "test.noise",
                                "params": {"value": "x"}}),
                )
                await asyncio.sleep(0.6)
                return queue.qsize()
            finally:
                harness.transport.unsubscribe(queue)
                writer.close()

        assert run(scenario()) >= 1
        assert any(frame["method"] == "test.noise" for frame in harness.transport.broadcast)


class TestStatic:
    def test_the_page_is_served_from_the_web_root(self, harness):
        response = run(harness.request("GET", "/"))
        assert response.status == 200
        assert "text/html" in response.headers["content-type"]
        assert "XLI" in response.body.decode("utf-8")

    def test_css_and_js_get_their_types(self, harness):
        css = run(harness.request("GET", "/style.css"))
        js = run(harness.request("GET", "/app.js"))
        assert "text/css" in css.headers["content-type"]
        assert "javascript" in js.headers["content-type"]

    def test_a_missing_asset_is_a_404_not_the_page(self, harness):
        response = run(harness.request("GET", "/нет.css"))
        assert response.status == 404

    def test_a_route_falls_back_to_the_page(self, harness):
        response = run(harness.request("GET", "/какая-то/страница"))
        assert response.status == 200
        assert "text/html" in response.headers["content-type"]

    def test_path_traversal_does_not_escape_the_root(self, harness):
        for attempt in ("/../pyproject.toml", "/%2e%2e/pyproject.toml", "/../../etc/passwd"):
            response = run(harness.request("GET", attempt))
            assert response.status in (200, 404)
            if response.status == 200:
                # Only the fallback page may be returned, never a real file.
                assert "text/html" in response.headers["content-type"]
            assert b"[project]" not in response.body

    def test_health_says_what_is_running(self, harness):
        payload = run(harness.request("GET", "/health")).json
        assert payload["ok"] is True
        assert payload["web"] is True
        assert payload["methods"] >= 3
