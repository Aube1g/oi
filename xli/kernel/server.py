#!/usr/bin/env python3
"""
XLI Kernel Server — transport-agnostic JSON-RPC method dispatcher.

The server knows nothing about sockets or stdin. It takes decoded frames and
returns frames to emit, so the same object can be driven by:

  * a stdio pipe      (the CLI / a Go frontend spawned as a subprocess)
  * a unix socket     (a long-lived daemon shared by nvim + terminal)
  * a direct call     (unit tests, the in-process TUI)

Methods are registered with `KernelServer.method("agent.run")`. Handlers may be
sync or async, and may declare the params they require — the server validates
the call before the handler runs, so a bad frame from a foreign client turns
into INVALID_PARAMS rather than a traceback halfway through.

Handlers can also be *streaming*: if a handler is an async generator, each
yielded dict is emitted as a notification on `method + ".stream"` and the final
value becomes the response result.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from typing import (
    Any,
)
from collections.abc import AsyncGenerator, Callable

from xli.kernel.protocol import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    Frame,
    IdAllocator,
    Notification,
    ProtocolError,
    Request,
    Response,
    decode,
    encode_line,
    error_response,
    negotiate,
)

Handler = Callable[..., Any]


@dataclass(slots=True)
class MethodSpec:
    name: str
    handler: Handler
    required: tuple[str, ...] = ()
    doc: str = ""
    stream: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "required": list(self.required),
            "stream": self.stream,
            "doc": self.doc,
        }


def _is_binding_error(handler, params: dict[str, Any], exc: TypeError) -> bool:
    """True when `exc` comes from binding `params` to `handler`, not its body.

    Distinguishes "you sent arguments this handler does not take" from a
    TypeError raised inside the handler. Only the former may be retried.
    """
    try:
        inspect.signature(handler).bind(**params)
    except TypeError:
        return True          # the arguments genuinely do not fit
    return False             # they fit, so the error came from the body


class KernelServer:
    """Dispatches JSON-RPC frames to registered handlers."""

    def __init__(self, name: str = "xli-kernel", protocol_version: int = PROTOCOL_VERSION):
        self.name = name
        self.protocol_version = protocol_version
        self._methods: dict[str, MethodSpec] = {}
        self._outbox: asyncio.Queue = asyncio.Queue()
        self.ids = IdAllocator()
        self.started_at = time.time()
        self.stats: dict[str, int] = {"requests": 0, "errors": 0, "notifications": 0}
        self.client: dict[str, Any] = {}
        self._register_introspection()

    # ------------------------------------------------------------ registration
    def method(
        self,
        name: str,
        *,
        required: tuple[str, ...] = (),
        doc: str = "",
    ) -> Callable[[Handler], Handler]:
        """Decorator registering a handler under a dotted method name."""

        def decorator(fn: Handler) -> Handler:
            self.register(name, fn, required=required, doc=doc or (fn.__doc__ or "").strip())
            return fn

        return decorator

    def register(
        self, name: str, fn: Handler, *, required: tuple[str, ...] = (), doc: str = ""
    ) -> None:
        if not name or " " in name:
            raise ValueError(f"invalid method name: {name!r}")
        stream = inspect.isasyncgenfunction(fn)
        self._methods[name] = MethodSpec(
            name=name, handler=fn, required=tuple(required), doc=doc.splitlines()[0] if doc else "",
            stream=stream,
        )

    def unregister(self, name: str) -> bool:
        return self._methods.pop(name, None) is not None

    def has_method(self, name: str) -> bool:
        return name in self._methods

    def list_methods(self) -> list[dict[str, Any]]:
        return [spec.schema() for spec in sorted(self._methods.values(), key=lambda s: s.name)]

    def _register_introspection(self) -> None:
        self.register("rpc.methods", self._rpc_methods, doc="List every registered method.")
        self.register("kernel.ping", self._kernel_ping, doc="Liveness probe.")
        self.register("kernel.stats", self._kernel_stats, doc="Uptime and call counters.")

    # ------------------------------------------------------------------ frames
    async def feed_line(self, raw: str | bytes | bytearray) -> list[Frame]:
        """Handle one inbound line, returning frames to send back (0..n)."""
        try:
            frame = decode(raw)
        except ProtocolError as exc:
            self.stats["errors"] += 1
            return [Response(id=None, error=exc.to_dict())]
        return await self.feed(frame)

    async def feed(self, frame: Frame) -> list[Frame]:
        if isinstance(frame, Response):
            # Responses to requests *we* sent — no reply is owed.
            return []
        if isinstance(frame, Notification):
            self.stats["notifications"] += 1
            return []
        return [await self.handle_request(frame)]

    async def handle_request(self, req: Request) -> Response:
        self.stats["requests"] += 1

        spec = self._methods.get(req.method)
        if spec is None:
            self.stats["errors"] += 1
            return Response(
                id=req.id,
                error={"code": METHOD_NOT_FOUND, "message": f"no such method: {req.method}"},
            )

        missing = [key for key in spec.required if key not in req.params]
        if missing:
            self.stats["errors"] += 1
            return Response(
                id=req.id,
                error={
                    "code": INVALID_PARAMS,
                    "message": f"{req.method}: missing required params: {', '.join(missing)}",
                },
            )

        try:
            result = await self._invoke(spec, req.params)
        except asyncio.CancelledError:
            self.stats["errors"] += 1
            raise
        except Exception as exc:  # noqa: BLE001 - protocol boundary must not leak
            self.stats["errors"] += 1
            return error_response(req.id, exc)

        if inspect.isawaitable(result):
            try:
                result = await result
            except Exception as exc:  # noqa: BLE001
                self.stats["errors"] += 1
                return error_response(req.id, exc)

        return Response(id=req.id, result=result)

    async def _invoke(self, spec: MethodSpec, params: dict[str, Any]) -> Any:
        """Call the handler; async generators are drained into notifications.

        Handlers may be written either as `def h(name, args)` or as
        `def h(params)`. Which one to use is decided by binding the arguments
        first, not by catching TypeError from the call.

        The old form was:

            try:
                outcome = handler(**params)
            except TypeError:
                outcome = handler(params)

        That could not tell a bad call from a TypeError raised inside the
        handler body, so a handler that failed after doing some work was run a
        second time — a mutation performed twice, a file written twice. The
        genuine error was then reported to the client as -32602 "invalid
        params", pointing at the caller for a server-side fault. Every
        registered handler is callable positionally, so all of them were
        exposed. Binding first removes the ambiguity: a binding failure is the
        only thing that triggers the positional form, and anything the body
        raises propagates untouched.
        """
        handler = spec.handler
        try:
            inspect.signature(handler).bind(**params)
            outcome = handler(**params)
        except TypeError as exc:
            if not _is_binding_error(handler, params, exc):
                raise
            outcome = handler(params)

        if inspect.isasyncgen(outcome):
            return await self._drain_stream(spec, outcome)
        return outcome

    async def _drain_stream(
        self, spec: MethodSpec, gen: AsyncGenerator[Any, None]
    ) -> dict[str, Any]:
        chunks = 0
        final: Any = None
        async for item in gen:
            if isinstance(item, dict) and "__final__" in item:
                final = item["__final__"]
                continue
            chunks += 1
            await self.notify(f"{spec.name}.stream", {"chunk": item, "index": chunks})
        return {"chunks": chunks, "result": final}

    # -------------------------------------------------------------- outbound
    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._outbox.put(Notification(method=method, params=params or {}))

    async def drain(self, timeout: float = 0.0) -> list[Frame]:
        """Collect everything queued for sending without blocking (or up to timeout)."""
        frames: list[Frame] = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 and frames:
                break
            try:
                frames.append(
                    self._outbox.get_nowait() if remaining <= 0 else await asyncio.wait_for(
                        self._outbox.get(), timeout=max(remaining, 0.001)
                    )
                )
            except asyncio.TimeoutError:
                break
            except asyncio.QueueEmpty:
                break
        return frames

    # ------------------------------------------------------------- built-ins
    def _rpc_methods(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "implementation": self.name,
            "methods": self.list_methods(),
        }

    def _kernel_ping(self) -> dict[str, Any]:
        return {"pong": True, "protocol_version": self.protocol_version, "name": self.name}

    def _kernel_stats(self) -> dict[str, Any]:
        return {
            "uptime_seconds": round(time.time() - self.started_at, 3),
            **self.stats,
            "methods": len(self._methods),
        }

    # ------------------------------------------------------------- handshake
    def accept_hello(self, params: dict[str, Any]) -> tuple[bool, Response]:
        """Validate a client's hello; returns (ok, response frame)."""
        client_version = int(params.get("protocol_version", 0))
        ok, server_hello = negotiate(client_version, self.protocol_version)
        server_hello["methods"] = len(self._methods)
        server_hello["name"] = self.name
        if ok:
            self.client = dict(params)
        else:
            server_hello["error"] = "protocol version mismatch"
        return ok, Response(id=params.get("_id"), result=server_hello)


# ----------------------------------------------------------------- helpers
def handshake_line(protocol_version: int = PROTOCOL_VERSION, client: str = "xli-cli") -> bytes:
    """Convenience: the first line a client writes after connecting."""
    req = Request(
        method="hello",
        id=0,
        params={"protocol_version": protocol_version, "client": client},
    )
    return encode_line(req)


def make_server(name: str = "xli-kernel") -> KernelServer:
    """Bare server with only introspection methods — used by tests and embedders."""
    return KernelServer(name=name)
