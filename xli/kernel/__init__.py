#!/usr/bin/env python3
"""
XLI Kernel — the JSON-RPC core every frontend talks to.

Public surface:
    KernelServer, protocol codec, transports.

Nothing here imports the LLM providers or the UIs, so the kernel can be
embedded in tests, driven from a subprocess, or fronted by a binary written in
another language.
"""

from xli.kernel.protocol import (
    CANCELLED,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    KERNEL_BUSY,
    KERNEL_ERROR,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PERMISSION_DENIED,
    PROTOCOL_VERSION,
    PROVIDER_ERROR,
    RATE_LIMITED,
    SESSION_NOT_FOUND,
    VERSION_MISMATCH,
    Frame,
    Notification,
    ProtocolError,
    Request,
    Response,
    decode,
    decode_many,
    decode_obj,
    encode,
    encode_line,
    error_response,
)
from xli.kernel.server import KernelServer, MethodSpec, make_server
from xli.kernel.daemon import serve_connection, serve_http, serve_stdio, serve_unix, socket_path, unix_client

__all__ = [
    "PROTOCOL_VERSION",
    "KernelServer",
    "MethodSpec",
    "make_server",
    "Request",
    "Response",
    "Notification",
    "Frame",
    "ProtocolError",
    "encode",
    "encode_line",
    "decode",
    "decode_obj",
    "decode_many",
    "error_response",
    "serve_connection",
    "serve_http",
    "serve_stdio",
    "serve_unix",
    "socket_path",
    "unix_client",
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
    "KERNEL_ERROR",
    "PERMISSION_DENIED",
    "PROVIDER_ERROR",
    "RATE_LIMITED",
    "SESSION_NOT_FOUND",
    "KERNEL_BUSY",
    "VERSION_MISMATCH",
    "CANCELLED",
]
