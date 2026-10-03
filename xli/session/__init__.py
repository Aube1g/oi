#!/usr/bin/env python3
"""
XLI Sessions — durable, resumable conversation history.

Storage is append-only JSONL, one event per line. That choice matters more than
it sounds: a whole-file rewrite (the obvious implementation) loses the tail of
a session if the process is killed mid-write, and makes concurrent writers
fight. Appending is atomic for anything under PIPE_BUF and lets a crashed run
be resumed from whatever actually reached disk.

Event kinds
-----------
  meta     written once at creation; carries model/provider/mode
  user     a prompt from the human
  assistant  model output, with any tool calls it made
  tool     a tool result, keyed by the call that produced it
  note     anything else worth replaying (permission decisions, errors)

A session is replayed into `messages` for the provider, and the same file is
also a human-readable transcript.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Iterator
from typing import Any

SESSION_DIRNAME = ".xli"
SESSION_SUBDIR = "sessions"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def estimate_tokens(text: str) -> int:
    """Cheap token estimate: ~4 chars per token for English, ~2 for CJK.

    Deliberately approximate — it drives truncation, not billing.
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u3000" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(other / 4 + cjk / 2)


@dataclass(slots=True)
class Event:
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    at: str = ""
    id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "at": self.at, "kind": self.kind, **self.data}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Event:
        payload = dict(raw)
        return cls(
            kind=str(payload.pop("kind", "note")),
            at=str(payload.pop("at", "")),
            id=str(payload.pop("id", "")),
            data=payload,
        )


class Session:
    """One conversation, persisted as append-only JSONL."""

    def __init__(
        self,
        session_id: str | None = None,
        *,
        root: Path | None = None,
        meta: dict[str, Any] | None = None,
    ):
        self.session_id = session_id or _new_id()
        self.root = Path(root) if root else Path.cwd()
        self.created_at = now_iso()
        self.events: list[Event] = []
        self.meta: dict[str, Any] = dict(meta or {})
        self._meta_written = False
        self._dirty = False
        self._token_estimate = 0

    # ------------------------------------------------------------------- paths
    @property
    def directory(self) -> Path:
        return self.root / SESSION_DIRNAME / SESSION_SUBDIR

    @property
    def path(self) -> Path:
        return self.directory / f"{self.session_id}.jsonl"

    # ------------------------------------------------------------------ writing
    def _append(self, kind: str, **data: Any) -> Event:
        # The meta header goes out before the first real event so every session
        # file self-describes (model/provider/mode) without a sidecar.
        if not self._meta_written:
            self._meta_written = True
            meta_event = Event(
                kind="meta",
                data={
                    "session_id": self.session_id,
                    "created_at": self.created_at,
                    **self.meta,
                },
                at=self.created_at,
                id=uuid.uuid4().hex[:12],
            )
            self.events.append(meta_event)
            self._write_event(meta_event)

        event = Event(kind=kind, data=data, at=now_iso(), id=uuid.uuid4().hex[:12])
        self.events.append(event)
        self._dirty = True
        self._token_estimate += estimate_tokens(str(data.get("content", "")))
        self._write_event(event)
        return event

    def _write_event(self, event: Event) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False, default=str)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass  # some filesystems reject fsync on append; the flush is enough

    def add_user(self, content: str) -> Event:
        return self._append("user", content=content)

    def add_assistant(
        self, content: str, calls: list[dict[str, Any]] | None = None
    ) -> Event:
        return self._append("assistant", content=content, calls=calls or [])

    def add_tool(self, name: str, result: Any, *, ok: bool = True) -> Event:
        return self._append("tool", name=name, ok=ok, result=result)

    def add_note(self, text: str, **extra: Any) -> Event:
        return self._append("note", content=text, **extra)

    # ------------------------------------------------------------------ reading
    @classmethod
    def load(cls, session_id: str, *, root: Path | None = None) -> Session:
        session = cls(session_id=session_id, root=root)
        path = session.path
        if not path.exists():
            raise FileNotFoundError(f"no such session: {path}")

        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    # A torn final line from a killed writer: skip, keep the rest.
                    continue
                event = Event.from_dict(raw)
                session.events.append(event)
                if event.kind == "meta":
                    session.meta = dict(event.data)
                    session.created_at = event.at or session.created_at
                    session._meta_written = True
                else:
                    session._token_estimate += estimate_tokens(str(event.data.get("content", "")))
        session._dirty = False
        return session

    @classmethod
    def list_sessions(cls, *, root: Path | None = None) -> list[dict[str, Any]]:
        """Newest-first summary of every stored session."""
        directory = Path(root or Path.cwd()) / SESSION_DIRNAME / SESSION_SUBDIR
        if not directory.is_dir():
            return []

        summaries: list[dict[str, Any]] = []
        for path in directory.glob("*.jsonl"):
            summary = {
                "id": path.stem,
                "path": str(path),
                "events": 0,
                "bytes": path.stat().st_size,
                "mtime_ns": path.stat().st_mtime_ns,
                "modified": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "first_prompt": "",
            }
            try:
                with open(path, encoding="utf-8") as handle:
                    for line in handle:
                        if not line.strip():
                            continue
                        summary["events"] += 1
                        if not summary["first_prompt"]:
                            try:
                                raw = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if raw.get("kind") == "user":
                                summary["first_prompt"] = str(raw.get("content", ""))[:120]
            except OSError:
                continue
            summaries.append(summary)

        # Nanosecond mtime, then id, so two sessions created in the same
        # instant still come back in a stable, predictable order. mtime_ns is a
        # sort key only — it is stripped before the summary is handed back.
        summaries.sort(key=lambda s: (s["mtime_ns"], s["id"]), reverse=True)
        for summary in summaries:
            summary.pop("mtime_ns", None)
        return summaries

    @classmethod
    def latest(cls, *, root: Path | None = None) -> Session | None:
        found = cls.list_sessions(root=root)
        return cls.load(found[0]["id"], root=root) if found else None

    def delete(self) -> bool:
        try:
            self.path.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    # -------------------------------------------------------------- projection
    def messages(self, *, max_tokens: int | None = None) -> list[dict[str, str]]:
        """Flatten events into the chat shape providers expect.

        With `max_tokens`, the oldest turns are dropped until the transcript
        fits — but the first user prompt is always kept, so the model never
        loses the original task.
        """
        messages: list[dict[str, str]] = []
        for event in self.events:
            if event.kind == "user":
                messages.append({"role": "user", "content": str(event.data.get("content", ""))})
            elif event.kind == "assistant":
                content = str(event.data.get("content", ""))
                calls = event.data.get("calls") or []
                if calls:
                    suffix = "\n".join(json.dumps(c, ensure_ascii=False) for c in calls)
                    content = f"{content}\n{suffix}".strip()
                messages.append({"role": "assistant", "content": content})
            elif event.kind == "tool":
                payload = event.data.get("result")
                text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
                status = "ok" if event.data.get("ok", True) else "error"
                messages.append({"role": "user", "content": f"[tool {event.data.get('name')}: {status}] {text}"})

        if max_tokens is None:
            return messages

        def total(items: list[dict[str, str]]) -> int:
            return sum(estimate_tokens(m["content"]) for m in items)

        while total(messages) > max_tokens and len(messages) > 2:
            # Drop from the front, but never the opening user prompt.
            del messages[1]
        return messages

    @property
    def token_estimate(self) -> int:
        return self._token_estimate

    def transcript(self) -> str:
        """Human-readable rendering, for `xli session show`."""
        lines: list[str] = [f"session {self.session_id}  ({len(self.events)} events)"]
        for event in self.events:
            if event.kind == "user":
                lines.append(f"\n  you: {event.data.get('content', '')}")
            elif event.kind == "assistant":
                text = str(event.data.get("content", "")).strip()
                lines.append(f"\n  xli: {text}" if text else "")
                for call in event.data.get("calls") or []:
                    lines.append(f"       -> {call.get('name')} {call.get('args', {})}")
            elif event.kind == "tool":
                status = "ok" if event.data.get("ok", True) else "FAIL"
                lines.append(f"       [{status}] {event.data.get('name')}")
        return "\n".join(line for line in lines if line is not None)

    def __len__(self) -> int:
        return len(self.events)

    def __iter__(self) -> Iterator[Event]:
        return iter(self.events)


def _new_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def session_root(project_dir: Path | None = None) -> Path:
    return Path(project_dir or Path.cwd()) / SESSION_DIRNAME / SESSION_SUBDIR
