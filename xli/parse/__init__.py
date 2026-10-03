#!/usr/bin/env python3
"""
XLI Parse — turning raw model output into executable tool calls.

Model output is not reliable JSON. This module is the damage-control layer, and
every heuristic in it exists because a real model produced that shape:

  * payloads nested inside fenced code blocks the model never closed
  * `{...}` blobs containing nested braces, which no regex can delimit — so we
    count depth and honour string escapes instead
  * single-backslash Windows paths (`C:\\Users\\me`) that are not valid JSON
  * trailing commas, single quotes, and unquoted keys from weaker models
  * a `<done>` marker with prose around it

When something has to be repaired to be readable, that repair is recorded in
`ParsedResponse.repairs` so the agent can tell the model what it did wrong —
which measurably reduces repeat offences in the next turn.

Streaming
---------
`StreamParser` accepts text incrementally and yields complete calls the moment
they are closed, so a UI can act on a tool call before the model has finished
talking.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# Built without a literal "</tool>" so this file never contains its own
# delimiter — otherwise the agent's own parser tests trip over their source.
_TAG = "tool"
_OPEN = "<" + _TAG + ">"
_CLOSE = "</" + _TAG + ">"
_DONE_OPEN = "<done>"
_DONE_CLOSE = "</done>"

# A backslash that cannot start a JSON escape — typically a Windows path the
# model wrote as "C:\Users\proj" instead of "C:\\Users\\proj".
_STRAY_BACKSLASH = re.compile(r'\\(?!["\\/bfnrtu])')

_FENCE = "```"


@dataclass(slots=True)
class ToolCall:
    name: str
    args: dict[str, Any]
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "args": self.args}


@dataclass(slots=True)
class ParsedResponse:
    text: str
    calls: list[ToolCall] = field(default_factory=list)
    done: bool = False
    done_text: str = ""
    repairs: list[str] = field(default_factory=list)

    @property
    def has_calls(self) -> bool:
        return bool(self.calls)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "calls": [c.to_dict() for c in self.calls],
            "done": self.done,
            "done_text": self.done_text,
            "repairs": self.repairs,
        }


# ------------------------------------------------------------------ json spans
def json_spans(text: str) -> list[tuple[int, int]]:
    """Every balanced ``{...}`` span, honouring string escapes.

    A regex cannot do this: tool payloads nest braces, so a non-greedy match
    stops at the first inner one. Counting depth while skipping quoted text
    reads both the simple and the nested shapes.
    """
    spans: list[tuple[int, int]] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":  # noqa: SIM102 - the append must only fire on the brace that CLOSED a span
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    spans.append((start, index + 1))
                    start = -1

    return spans


def strip_fences(text: str) -> tuple[str, bool]:
    """Remove ```json ... ``` wrappers, including an unterminated one."""
    if _FENCE not in text:
        return text, False

    repaired = False
    # An unterminated trailing fence is the common case; close it implicitly.
    if text.count(_FENCE) % 2 == 1:
        repaired = True

    pattern = re.compile(_FENCE + r"[a-zA-Z]*\n?(.*?)" + _FENCE, re.DOTALL)
    stripped = pattern.sub(lambda m: m.group(1), text)

    # Any fence left behind was never closed — drop it.
    if _FENCE in stripped:
        stripped = stripped.replace(_FENCE, "")
        repaired = True

    return stripped, repaired


def loads_lenient(payload: str) -> tuple[Any, list[str]]:
    """json.loads that survives the usual model sins.

    Returns (value, repairs). Raises json.JSONDecodeError only when nothing
    can be salvaged.
    """
    repairs: list[str] = []

    try:
        return json.loads(payload), repairs
    except json.JSONDecodeError:
        pass

    # 1. Stray single backslashes (Windows paths).
    candidate = _STRAY_BACKSLASH.sub(r"\\\\", payload)
    if candidate != payload:
        repairs.append("escaped stray backslashes")
        try:
            return json.loads(candidate), repairs
        except json.JSONDecodeError:
            pass

    # 2. Trailing commas before } or ].
    cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
    if cleaned != candidate:
        repairs.append("removed trailing commas")
        try:
            return json.loads(cleaned), repairs
        except json.JSONDecodeError:
            pass

    # 3. Single-quoted strings and unquoted keys — last resort, and only when
    #    the payload looks like a single flat object.
    converted = re.sub(r"'([^'\\]*)'", r'"\1"', cleaned)
    converted = re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:', r'\1"\2":', converted)
    if converted != cleaned:
        repairs.append("normalised quotes and keys")
        return json.loads(converted), repairs

    raise json.JSONDecodeError("unparseable tool payload", payload, 0)


# -------------------------------------------------------------------- parsing
def extract_tool_payloads(text: str) -> list[tuple[str, int, int]]:
    """Return (payload, start, end) for every ``<tool>...</tool>`` block."""
    out: list[tuple[str, int, int]] = []
    cursor = 0
    while True:
        open_at = text.find(_OPEN, cursor)
        if open_at < 0:
            break
        close_at = text.find(_CLOSE, open_at + len(_OPEN))
        if close_at < 0:
            # Unterminated: take the rest, the stream may still be running.
            out.append((text[open_at + len(_OPEN) :], open_at, len(text)))
            break
        out.append(
            (text[open_at + len(_OPEN) : close_at], open_at, close_at + len(_CLOSE))
        )
        cursor = close_at + len(_CLOSE)
    return out


#: The only keys a tool-call object ever carries. When no tool catalogue is
#: available to validate names against, an untaged JSON object is accepted as
#: a call only if its keys stay inside this set — prose that merely *shows*
#: JSON almost always carries other keys, so this kills the false positives.
_CALL_KEYS = frozenset({"name", "tool", "args", "arguments", "params"})


def extract_bare_calls(
    prose: str, known_tools: set[str] | None = None
) -> tuple[list[ToolCall], str, list[str]]:
    """Pull tool-call JSON the model wrote *without* the ``<tool>`` wrapper.

    Weaker models (and stronger ones on bad days) emit ``{"name": "read",
    "args": {...}}`` as plain text. Without this fallback that JSON reaches
    the user verbatim and the loop stops with ``no_tool_calls`` — the single
    most confusing failure an agent can show. Returns the calls, the prose
    with the call objects removed, and the repairs to report.

    With `known_tools` a candidate's name must be a registered tool; without
    it the object's keys must be a subset of the call shape.
    """
    calls: list[ToolCall] = []
    repairs: list[str] = []
    if "{" not in prose:
        return calls, prose, repairs

    removed: list[tuple[int, int]] = []
    for start, end in json_spans(prose):
        payload = prose[start:end]
        try:
            value, _repairs = loads_lenient(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        name = value.get("name") or value.get("tool")
        if not isinstance(name, str) or not name.strip():
            continue
        args = value.get("args") or value.get("arguments") or value.get("params") or {}
        if not isinstance(args, dict):
            continue
        if known_tools is not None:
            if name not in known_tools:
                continue
        elif set(value) - _CALL_KEYS:
            continue

        calls.append(ToolCall(name=name.strip(), args=args, raw=payload))
        removed.append((start, end))
        repairs.append(f"extracted an untagged tool call ({name.strip()})")

    for start, end in reversed(removed):
        prose = prose[:start] + prose[end:]

    return calls, prose.strip(), repairs


def parse_response(text: str, *, known_tools: set[str] | None = None) -> ParsedResponse:
    """Parse a complete model turn into text + tool calls + done marker."""
    repairs: list[str] = []
    body, fenced = strip_fences(text)
    if fenced:
        repairs.append("closed an unterminated code fence")

    calls: list[ToolCall] = []
    spans = extract_tool_payloads(body)

    for payload, start, _end in spans:
        payload = payload.strip()
        if not payload:
            repairs.append("ignored an empty <tool> block")
            continue

        candidates = [payload]
        # The model sometimes writes prose inside the tag around the JSON.
        found = json_spans(payload)
        if found:
            candidates = [payload[s:e] for s, e in found] + [payload]

        parsed: Any | None = None
        for candidate in candidates:
            try:
                value, candidate_repairs = loads_lenient(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                parsed = value
                repairs.extend(candidate_repairs)
                break

        if parsed is None:
            repairs.append(f"skipped an unparseable <tool> block at offset {start}")
            continue

        name = parsed.get("name") or parsed.get("tool")
        args = parsed.get("args") or parsed.get("arguments") or parsed.get("params") or {}
        if not isinstance(name, str) or not name:
            repairs.append("skipped a tool block with no 'name'")
            continue
        if not isinstance(args, dict):
            repairs.append(f"coerced non-object args for {name}")
            args = {}

        calls.append(ToolCall(name=name, args=args, raw=payload))

    # Prose is everything outside the tool blocks and the done marker.
    prose = body
    for _payload, start, end in reversed(spans):
        prose = prose[:start] + prose[end:]

    done_text = ""
    done = False
    done_at = prose.find(_DONE_OPEN)
    if done_at >= 0:
        done_end = prose.find(_DONE_CLOSE, done_at)
        if done_end >= 0:
            done_text = prose[done_at + len(_DONE_OPEN) : done_end].strip()
            prose = prose[:done_at] + prose[done_end + len(_DONE_CLOSE) :]
            done = True
        else:
            # Unterminated <done> — treat as done with whatever we have.
            done_text = prose[done_at + len(_DONE_OPEN) :].strip()
            prose = prose[:done_at]
            done = True
            repairs.append("closed an unterminated <done> tag")

    bare, prose, bare_repairs = extract_bare_calls(prose, known_tools)
    calls.extend(bare)
    repairs.extend(bare_repairs)

    return ParsedResponse(
        text=prose.strip(), calls=calls, done=done, done_text=done_text, repairs=repairs
    )


class StreamParser:
    """Incremental parser: feed text, get complete calls as soon as they close.

    A call is emitted only once its ``</tool>`` has arrived, so a UI never acts
    on half a payload. Text is retained until the next boundary so prose is not
    duplicated across chunks.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._emitted = 0

    def feed(self, chunk: str) -> list[ToolCall]:
        """Add text and return any newly completed tool calls."""
        self._buffer += chunk
        spans = extract_tool_payloads(self._buffer)
        complete = [s for s in spans if s[0] != "" or True]

        fresh: list[ToolCall] = []
        for index, (payload, _start, _end) in enumerate(complete):
            if index < self._emitted:
                continue
            # The final span may be unterminated (stream still running).
            terminator = self._buffer.find(_CLOSE, _start)
            if terminator < 0:
                break
            self._emitted = index + 1
            call = _parse_single(payload)
            if call is not None:
                fresh.append(call)
        return fresh

    @property
    def pending(self) -> str:
        """Text buffered so far, with completed tool blocks removed."""
        prose = self._buffer
        for _payload, start, end in reversed(extract_tool_payloads(prose)):
            prose = prose[:start] + prose[end:]
        return prose.strip()

    def finish(self, *, known_tools: set[str] | None = None) -> ParsedResponse:
        """Parse everything remaining, including a trailing done marker."""
        result = parse_response(self._buffer, known_tools=known_tools)
        # Only report calls we have not already handed out.
        result.calls = result.calls[self._emitted :] if self._emitted else result.calls
        return result

    def reset(self) -> None:
        self._buffer = ""
        self._emitted = 0


def _parse_single(payload: str) -> ToolCall | None:
    payload = payload.strip()
    if not payload:
        return None
    candidates = [payload]
    found = json_spans(payload)
    if found:
        candidates = [payload[s:e] for s, e in found] + [payload]

    for candidate in candidates:
        try:
            value, _ = loads_lenient(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        name = value.get("name") or value.get("tool")
        if not isinstance(name, str) or not name:
            continue
        args = value.get("args") or value.get("arguments") or value.get("params") or {}
        if not isinstance(args, dict):
            args = {}
        return ToolCall(name=name, args=args, raw=payload)
    return None


def render_call(call: ToolCall) -> str:
    """Re-serialise a call the way the agent will echo it back to the model."""
    return f"{_OPEN}{json.dumps(call.to_dict(), ensure_ascii=False)}{_CLOSE}"
