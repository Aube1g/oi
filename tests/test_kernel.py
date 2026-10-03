#!/usr/bin/env python3
"""Tests for the JSON-RPC kernel: codec, dispatch, streaming, transports."""

import asyncio
import json

import pytest

from xli.kernel import (
    INVALID_PARAMS,
    KERNEL_ERROR,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    KernelServer,
    Notification,
    ProtocolError,
    Request,
    Response,
    decode,
    decode_many,
    encode,
    encode_line,
)


# ---------------------------------------------------------------------- codec
class TestCodec:
    def test_request_roundtrip(self):
        frame = Request(method="agent.run", id=7, params={"task": "fix it"})
        restored = decode(encode(frame))
        assert isinstance(restored, Request)
        assert restored.method == "agent.run"
        assert restored.id == 7
        assert restored.params == {"task": "fix it"}

    def test_encode_produces_single_line_no_trailing_newline(self):
        line = encode(Notification(method="x.y", params={"a": 1}))
        assert b"\n" not in line
        assert encode_line(Notification(method="x.y")).endswith(b"\n")

    def test_non_ascii_survives_roundtrip(self):
        frame = Request(method="chat", id=1, params={"text": "почини всё 🐝"})
        assert decode(encode(frame)).params["text"] == "почини всё 🐝"

    def test_response_with_result_has_no_error_key(self):
        payload = json.loads(encode(Response(id=3, result={"ok": True})))
        assert payload == {"jsonrpc": "2.0", "id": 3, "result": {"ok": True}}
        assert "error" not in payload

    def test_notification_has_no_id(self):
        payload = json.loads(encode(Notification(method="agent.progress")))
        assert "id" not in payload

    def test_malformed_json_raises_parse_error(self):
        with pytest.raises(ProtocolError) as exc:
            decode("{not json")
        assert exc.value.code == PARSE_ERROR

    def test_wrong_jsonrpc_version_rejected(self):
        with pytest.raises(ProtocolError):
            decode('{"jsonrpc":"1.0","id":1,"method":"x"}')

    def test_params_must_be_object(self):
        with pytest.raises(ProtocolError):
            decode('{"jsonrpc":"2.0","id":1,"method":"x","params":[1,2]}')

    def test_frame_with_neither_method_nor_result_rejected(self):
        with pytest.raises(ProtocolError):
            decode('{"jsonrpc":"2.0","id":1}')

    def test_unserializable_params_raise_protocol_error(self):
        with pytest.raises(ProtocolError):
            encode(Request(method="x", id=1, params={"bad": object()}))

    def test_decode_many_skips_blanks(self):
        blob = encode_line(Request(method="a", id=1)) + b"\n\n" + encode_line(Request(method="b", id=2))
        methods = [f.method for f in decode_many(blob)]
        assert methods == ["a", "b"]


# ----------------------------------------------------------------- dispatch
class TestDispatch:
    def test_sync_handler(self):
        server = KernelServer()

        @server.method("math.add")
        def add(a: int, b: int) -> int:
            return a + b

        out = asyncio.run(server.feed_line(encode_line(Request(method="math.add", id=1, params={"a": 2, "b": 3}))))
        assert len(out) == 1
        assert out[0].result == 5

    def test_async_handler(self):
        server = KernelServer()

        @server.method("slow.echo")
        async def echo(text: str) -> str:
            await asyncio.sleep(0)
            return text

        out = asyncio.run(server.feed_line(encode_line(Request(method="slow.echo", id=1, params={"text": "hi"}))))
        assert out[0].result == "hi"

    def test_unknown_method_gives_method_not_found(self):
        server = KernelServer()
        out = asyncio.run(server.feed_line(encode_line(Request(method="nope", id=1, params={}))))
        assert out[0].error["code"] == METHOD_NOT_FOUND

    def test_missing_required_param_gives_invalid_params(self):
        server = KernelServer()

        @server.method("needs.it", required=("thing",))
        def needs(thing):
            return thing

        out = asyncio.run(server.feed_line(encode_line(Request(method="needs.it", id=1, params={}))))
        assert out[0].error["code"] == INVALID_PARAMS
        assert "thing" in out[0].error["message"]

    def test_handler_exception_becomes_kernel_error_not_crash(self):
        server = KernelServer()

        @server.method("boom")
        def boom():
            raise RuntimeError("kaboom")

        out = asyncio.run(server.feed_line(encode_line(Request(method="boom", id=1, params={}))))
        assert out[0].error["code"] == KERNEL_ERROR
        assert "kaboom" in out[0].error["message"]

    def test_value_error_maps_to_invalid_params(self):
        server = KernelServer()

        @server.method("strict")
        def strict():
            raise ValueError("bad value")

        out = asyncio.run(server.feed_line(encode_line(Request(method="strict", id=1, params={}))))
        assert out[0].error["code"] == INVALID_PARAMS

    def test_handler_may_take_single_params_dict(self):
        server = KernelServer()

        @server.method("positional")
        def positional(params):
            return params["n"] * 2

        out = asyncio.run(server.feed_line(encode_line(Request(method="positional", id=1, params={"n": 21}))))
        assert out[0].result == 42

    def test_notification_produces_no_reply(self):
        server = KernelServer()
        out = asyncio.run(server.feed_line(encode_line(Notification(method="whatever"))))
        assert out == []
        assert server.stats["notifications"] == 1

    def test_inbound_response_is_ignored(self):
        server = KernelServer()
        out = asyncio.run(server.feed_line(encode_line(Response(id=9, result="ok"))))
        assert out == []

    def test_garbage_line_does_not_raise(self):
        server = KernelServer()
        out = asyncio.run(server.feed_line(b"!!! not json"))
        assert out[0].error["code"] == PARSE_ERROR

    def test_stats_track_requests_and_errors(self):
        server = KernelServer()

        @server.method("ok")
        def ok():
            return 1

        asyncio.run(server.feed_line(encode_line(Request(method="ok", id=1, params={}))))
        asyncio.run(server.feed_line(encode_line(Request(method="missing", id=2, params={}))))
        assert server.stats["requests"] == 2
        assert server.stats["errors"] == 1


# ------------------------------------------------------------------ streaming
class TestStreaming:
    def test_async_generator_emits_notifications_then_result(self):
        server = KernelServer()

        @server.method("agent.stream")
        async def stream():
            for i in range(3):
                yield {"token": f"t{i}"}
            yield {"__final__": "done"}

        out = asyncio.run(server.feed_line(encode_line(Request(method="agent.stream", id=1, params={}))))
        assert out[0].result == {"chunks": 3, "result": "done"}

    def test_queued_notifications_are_drainable(self):
        server = KernelServer()

        @server.method("agent.work")
        async def work():
            await server.notify("agent.progress", {"pct": 50})
            return "finished"

        asyncio.run(server.feed_line(encode_line(Request(method="agent.work", id=1, params={}))))

        async def collect():
            return await server.drain(timeout=0.05)

        frames = asyncio.run(collect())
        assert [f.method for f in frames] == ["agent.progress"]
        assert frames[0].params == {"pct": 50}


# -------------------------------------------------------------- introspection
class TestIntrospection:
    def test_rpc_methods_lists_registered_methods(self):
        server = KernelServer()

        @server.method("custom.thing", doc="Does a thing.")
        def thing():
            return None

        out = asyncio.run(server.feed_line(encode_line(Request(method="rpc.methods", id=1, params={}))))
        names = [m["name"] for m in out[0].result["methods"]]
        assert "custom.thing" in names
        assert "kernel.ping" in names
        assert out[0].result["protocol_version"] == PROTOCOL_VERSION

    def test_ping_and_stats(self):
        server = KernelServer()
        ping = asyncio.run(server.feed_line(encode_line(Request(method="kernel.ping", id=1, params={}))))
        assert ping[0].result["pong"] is True

        stats = asyncio.run(server.feed_line(encode_line(Request(method="kernel.stats", id=2, params={}))))
        assert stats[0].result["requests"] == 2
        assert stats[0].result["methods"] >= 3

    def test_method_docstring_becomes_schema_doc(self):
        server = KernelServer()

        @server.method("documented")
        def documented():
            """First line is the doc.

            The rest is ignored.
            """
            return None

        spec = server.list_methods()
        entry = next(m for m in spec if m["name"] == "documented")
        assert entry["doc"] == "First line is the doc."

    def test_register_rejects_bad_names(self):
        server = KernelServer()
        with pytest.raises(ValueError):
            server.register("has space", lambda: None)

    def test_unregister(self):
        server = KernelServer()
        server.register("tmp.thing", lambda: 1)
        assert server.has_method("tmp.thing")
        assert server.unregister("tmp.thing") is True
        assert server.has_method("tmp.thing") is False


# ----------------------------------------------------------------- transports
class TestTransports:
    def test_serve_connection_end_to_end(self):
        """Drive serve_connection with an in-memory pipe, like a Go client would."""
        from xli.kernel.daemon import serve_connection

        server = KernelServer()

        @server.method("echo")
        def echo(text):
            return text

        inbox = asyncio.Queue()
        for line in (
            encode_line(Request(method="echo", id=1, params={"text": "a"})),
            encode_line(Request(method="echo", id=2, params={"text": "b"})),
            b"",  # EOF
        ):
            inbox.put_nowait(line)

        sent = []

        async def read_line():
            return await inbox.get()

        async def write(payload):
            sent.append(payload)

        asyncio.run(serve_connection(server, read_line, write))

        assert len(sent) == 2
        assert [json.loads(p)["result"] for p in sent] == ["a", "b"]

    def test_serve_connection_survives_garbage_then_continues(self):
        from xli.kernel.daemon import serve_connection

        server = KernelServer()

        @server.method("ok")
        def ok():
            return "alive"

        inbox = asyncio.Queue()
        for line in (b"%%% broken", encode_line(Request(method="ok", id=1, params={})), b""):
            inbox.put_nowait(line)

        sent = []

        async def read_line():
            return await inbox.get()

        async def write(payload):
            sent.append(payload)

        asyncio.run(serve_connection(server, read_line, write))

        assert len(sent) == 2
        assert json.loads(sent[0])["error"]["code"] == PARSE_ERROR
        assert json.loads(sent[1])["result"] == "alive"

    def test_unix_socket_roundtrip(self, tmp_path):
        """A real client over a real socket — the path an external frontend uses."""
        from xli.kernel.daemon import serve_unix, unix_client

        server = KernelServer()

        @server.method("tools.count")
        def count():
            return 3

        sock = tmp_path / "kernel.sock"
        stop = asyncio.Event()

        async def scenario():
            server_task = asyncio.create_task(serve_unix(server, sock, stop_event=stop))
            await asyncio.sleep(0.1)  # let the listener bind

            reader, writer = await unix_client(sock)
            writer.write(encode_line(Request(method="tools.count", id=1, params={})))
            await writer.drain()

            line = await asyncio.wait_for(reader.readline(), timeout=5)
            writer.close()
            stop.set()
            await server_task
            return json.loads(line)

        reply = asyncio.run(scenario())
        assert reply["result"] == 3
        assert not sock.exists(), "socket file must be cleaned up on shutdown"

    def test_stale_socket_file_is_replaced(self, tmp_path):
        import stat

        from xli.kernel.daemon import serve_unix

        sock = tmp_path / "kernel.sock"
        sock.write_text("stale")
        stop = asyncio.Event()

        async def scenario():
            task = asyncio.create_task(serve_unix(server=KernelServer(), path=sock, stop_event=stop))
            await asyncio.sleep(0.1)
            # If the stale regular file had not been unlinked, bind() would have
            # raised and the path would still be a plain file containing "stale".
            is_socket = stat.S_ISSOCK(sock.stat().st_mode)
            stop.set()
            await task
            return is_socket

        assert asyncio.run(scenario()) is True

    def test_socket_path_defaults(self):
        from pathlib import Path

        from xli.kernel.daemon import socket_path

        assert socket_path("kernel").name == "kernel.sock"
        custom_dir = Path("/tmp/xli-test-runtime")
        assert socket_path("custom", runtime_dir=custom_dir) == custom_dir / "custom.sock"


class TestHandlerIsNotRunTwice:
    """A TypeError from a handler's body used to re-invoke it.

    _invoke did:
        try:    handler(**params)
        except TypeError: handler(params)
    which could not tell a bad call from a TypeError raised inside the handler.
    A handler that failed after doing work was therefore run a second time, and
    the real error was reported as -32602, blaming the caller. Every registered
    handler is callable positionally, so all of them were exposed.
    """

    def test_a_body_typeerror_does_not_reinvoke_the_handler(self):
        server = KernelServer()
        calls = []

        @server.method("mutating")
        def mutating(a=1):
            calls.append(a)
            return None + 1  # TypeError from the body

        out = asyncio.run(
            server.feed_line(encode_line(Request(method="mutating", id=1, params={"a": 1})))
        )
        assert len(calls) == 1, f"handler ran {len(calls)} times; must run once"
        assert out[0].error is not None

    def test_the_positional_params_form_is_still_supported(self):
        """The fallback exists for `def h(params)` handlers and must survive."""
        server = KernelServer()

        @server.method("positional")
        def positional(params):
            return {"got": params}

        out = asyncio.run(
            server.feed_line(encode_line(Request(method="positional", id=1, params={"x": 1})))
        )
        assert out[0].result == {"got": {"x": 1}}

    def test_a_handler_taking_no_arguments_is_not_retried(self):
        """The real kernel.clean case: zero-arg handler, caller sends params.

        Neither form fits, so the call must fail once and say so, rather than
        retrying into a second, differently-shaped failure.
        """
        server = KernelServer()
        calls = []

        @server.method("zero_args")
        def zero_args():
            calls.append(1)
            return "ran"

        out = asyncio.run(
            server.feed_line(encode_line(Request(method="zero_args", id=1, params={"a": 1})))
        )
        assert out[0].error is not None
        assert calls == [], "the body must not run when no form of the call fits"

    def test_the_real_kernel_clean_rejects_unwanted_params_cleanly(self):
        """kernel.clean takes no arguments; this is what the sweep found."""
        from xli.kernel.methods import build_kernel

        server = build_kernel()
        out = asyncio.run(
            server.feed_line(
                encode_line(Request(method="kernel.clean", id=1, params={"dry_run": True}))
            )
        )
        assert out[0].error is not None
        assert "positional" in out[0].error["message"]


class TestTypeErrorIsNotBlamedOnTheCaller:
    """A broken handler is a server fault, not invalid params."""

    def test_typeerror_maps_to_kernel_error(self):
        from xli.kernel.protocol import KERNEL_ERROR

        server = KernelServer()

        @server.method("broken")
        def broken():
            return None + 1  # TypeError

        out = asyncio.run(server.feed_line(encode_line(Request(method="broken", id=1, params={}))))
        assert out[0].error["code"] == KERNEL_ERROR

    def test_valueerror_still_maps_to_invalid_params(self):
        """Handlers validate input with ValueError; that contract is unchanged."""
        from xli.kernel.protocol import INVALID_PARAMS

        server = KernelServer()

        @server.method("validating")
        def validating():
            raise ValueError("bad value")

        out = asyncio.run(
            server.feed_line(encode_line(Request(method="validating", id=1, params={})))
        )
        assert out[0].error["code"] == INVALID_PARAMS

    def test_missing_required_params_still_reports_invalid_params(self):
        """The explicit required-params path is untouched by the mapping change."""
        from xli.kernel.protocol import INVALID_PARAMS

        server = KernelServer()

        @server.method("needs.both", required=("a", "b"))
        def needs_both(a, b):
            return [a, b]

        out = asyncio.run(
            server.feed_line(encode_line(Request(method="needs.both", id=1, params={"a": 1})))
        )
        assert out[0].error["code"] == INVALID_PARAMS
        assert "b" in out[0].error["message"]
