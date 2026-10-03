#!/usr/bin/env python3
"""
XLI Kernel Protocol — JSON-RPC 2.0 codec.

This module is deliberately dependency-free (stdlib only) and free of any
`xli.*` imports. It defines the *wire contract* between the XLI kernel and any
frontend — the Python CLI, the TUI, the Neovim bridge, or a Go/Rust binary
written later. Anything that speaks these bytes can drive the agent.

Transport is newline-delimited JSON ("ndjson"): one JSON object per line,
UTF-8, no embedded newlines. That is trivial to consume from any language and
keeps streams greppable and replayable.

Frame kinds
-----------
Request       {"jsonrpc":"2.0","id":<int>,"method":"agent.run","params":{...}}
Response      {"jsonrpc":"2.0","id":<int>,"result":{...}}
Error         {"jsonrpc":"2.0","id":<int>,"error":{"code":<int>,"message":str,"data":...}}
Notification  {"jsonrpc":"2.0","method":"agent.progress","params":{...}}   (no id)

Protocol version
----------------
Every connection starts with a `hello` handshake; both sides state the
protocol version they speak so an older Go frontend talking to a newer kernel
fails loudly at connect time instead of mid-session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Union
from collections.abc import Iterator

PROTOCOL_VERSION = 1
JSONRPC = "2.0"

# ---------------------------------------------------------------- error codes
# -32700..-32603 are reserved by the JSON-RPC spec; everything below -32000 is
# ours. Keeping them in one place means a Go client can mirror the table.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

KERNEL_ERROR = -32000          # generic kernel-side failure
PERMISSION_DENIED = -32001     # a tool call was refused by the policy
PROVIDER_ERROR = -32002        # the LLM endpoint failed
RATE_LIMITED = -32003
SESSION_NOT_FOUND = -32004
KERNEL_BUSY = -32005
VERSION_MISMATCH = -32006
CANCELLED = -32007


class ProtocolError(Exception):
    """Raised when a frame cannot be understood or is semantically invalid."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err


# --------------------------------------------------------------------- frames
@dataclass(slots=True)
class Request:
    method: str
    id: int | str
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"jsonrpc": JSONRPC, "id": self.id, "method": self.method, "params": self.params}


@dataclass(slots=True)
class Notification:
    method: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"jsonrpc": JSONRPC, "method": self.method, "params": self.params}


@dataclass(slots=True)
class Response:
    id: int | str | None
    result: Any = None
    error: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        frame: dict[str, Any] = {"jsonrpc": JSONRPC, "id": self.id}
        if self.error is not None:
            frame["error"] = self.error
        else:
            frame["result"] = self.result
        return frame


Frame = Union[Request, Response, Notification]


# ---------------------------------------------------------------------- codec
def encode(frame: Frame) -> bytes:
    """Serialize a frame to one ndjson line (no trailing newline)."""
    payload = frame.to_dict()
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        # Never let one unserializable param kill the connection.
        raise ProtocolError(
            INTERNAL_ERROR, f"frame is not JSON-serializable: {exc}", payload.get("method")
        ) from exc


def encode_line(frame: Frame) -> bytes:
    return encode(frame) + b"\n"


def decode(raw: str | bytes | bytearray) -> Frame:
    """Parse one ndjson line into a typed frame."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(PARSE_ERROR, f"frame is not valid UTF-8: {exc}") from exc

    raw = raw.strip()
    if not raw:
        raise ProtocolError(PARSE_ERROR, "empty frame")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(PARSE_ERROR, f"malformed JSON: {exc}") from exc

    return decode_obj(data)


def decode_obj(data: Any) -> Frame:
    if not isinstance(data, dict):
        raise ProtocolError(INVALID_REQUEST, "frame must be a JSON object")
    if data.get("jsonrpc") != JSONRPC:
        raise ProtocolError(
            INVALID_REQUEST,
            f"unsupported jsonrpc version: {data.get('jsonrpc')!r} (expected {JSONRPC!r})",
        )

    method = data.get("method")
    if isinstance(method, str):
        params = data.get("params") or {}
        if not isinstance(params, dict):
            raise ProtocolError(INVALID_PARAMS, "params must be an object")
        if "id" in data:
            return Request(method=method, id=data["id"], params=params)
        return Notification(method=method, params=params)

    if "result" in data or "error" in data:
        error = data.get("error")
        if error is not None and not isinstance(error, dict):
            raise ProtocolError(INVALID_REQUEST, "error must be an object")
        return Response(id=data.get("id"), result=data.get("result"), error=error)

    raise ProtocolError(INVALID_REQUEST, "frame has neither a method nor a result/error")


def decode_many(raw: str | bytes | bytearray) -> Iterator[Frame]:
    """Yield every frame contained in a buffer, skipping blank lines."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    for line in raw.splitlines():
        if line.strip():
            yield decode(line)


def error_response(req_id: int | str | None, exc: BaseException) -> Response:
    """Build a Response carrying an error, mapping known exceptions to codes."""
    if isinstance(exc, ProtocolError):
        return Response(id=req_id, error=exc.to_dict())
    if isinstance(exc, PermissionError):
        return Response(id=req_id, error={"code": PERMISSION_DENIED, "message": str(exc)})
    if isinstance(exc, KeyError):
        return Response(id=req_id, error={"code": METHOD_NOT_FOUND, "message": str(exc)})
    if isinstance(exc, ValueError):
        # Handlers validate their input by raising ValueError, so this one means
        # the caller sent something unusable.
        return Response(id=req_id, error={"code": INVALID_PARAMS, "message": str(exc)})
    # TypeError deliberately does NOT map to INVALID_PARAMS. Nothing in the
    # kernel raises it for bad input; it means the handler itself is broken, and
    # reporting that as the caller's fault sends whoever is debugging down the
    # wrong path. Bad arguments are caught earlier, by the required-params check
    # and by signature binding in _invoke.
    return Response(id=req_id, error={"code": KERNEL_ERROR, "message": f"{type(exc).__name__}: {exc}"})


# ----------------------------------------------------------------- handshake
def hello_frame(
    protocol_version: int = PROTOCOL_VERSION,
    client: str = "unknown",
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    """Payload for the `hello` request a client sends first."""
    return {
        "protocol_version": protocol_version,
        "client": client,
        "capabilities": capabilities or [],
    }


def negotiate(
    client_version: int, server_version: int = PROTOCOL_VERSION
) -> tuple[bool, dict[str, Any]]:
    """Compare versions; returns (compatible, server_hello).

    Compatibility is major-version equality. A client from the future is told
    the server's version so it can decide to downgrade or refuse.
    """
    return (
        client_version == server_version,
        {"protocol_version": server_version, "implementation": "xli-kernel"},
    )


# --------------------------------------------------------------- id generator
class IdAllocator:
    """Monotonic ids for outbound requests, safe to share across coroutines."""

    def __init__(self, start: int = 1):
        self._next = start

    def __call__(self) -> int:
        value = self._next
        self._next += 1
        return value

    def __iter__(self):
        return self

    def __next__(self) -> int:
        return self()
