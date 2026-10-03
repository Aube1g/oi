#!/usr/bin/env python3
"""
XPI State — key/value state shared across UI platforms.

The point is continuity: the TUI, the Neovim frontend and a headless run are
different processes, but a plugin watching all three wants one place to keep
what it has learned. This is that place — `~/.xli/xpi_state.json`.

Writes are atomic (write a temp file, then rename) because this file is shared
and a half-written JSON document would silently wipe everything on next load.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from xli.paths import xli_path
from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.xpi.state")

STATE_FILE = xli_path("xpi_state.json")


class XpiState:
    """Shared, persisted state. Singleton per process."""

    _instance: XpiState | None = None
    _lock = threading.Lock()

    def __new__(cls, *args: Any, **kwargs: Any) -> XpiState:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, path: Path | None = None):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        self.path = Path(path) if path else STATE_FILE
        self._state: dict[str, Any] = {}
        self._write_lock = threading.Lock()
        self._load()
        logger.log_structured("INFO", "xpi.state", f"loaded {len(self._state)} key(s)")

    @classmethod
    def reset(cls) -> None:
        """Drop the singleton so the next call re-reads from disk."""
        cls._instance = None

    # --------------------------------------------------------------- storage
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # Keep going with an empty state rather than refusing to start.
            logger.log_error("xpi.state", f"could not read {self.path}", exc=exc)
            return
        if isinstance(raw, dict):
            self._state = raw
        else:
            logger.log_structured(
                "WARN", "xli.state", f"{self.path} is not an object — ignoring"
            )

    def _save(self) -> None:
        with self._write_lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                tmp.write_text(
                    json.dumps(self._state, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                # rename is atomic on POSIX, so a reader never sees a partial file
                os.replace(tmp, self.path)
            except OSError as exc:
                logger.log_error("xli.state", "save failed", exc=exc)

    # ------------------------------------------------------------------ api
    def get(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._state[key] = value
        self._save()

    def update(self, values: dict[str, Any]) -> None:
        """Set several keys with a single write."""
        self._state.update(values)
        self._save()

    def delete(self, key: str) -> bool:
        if key not in self._state:
            return False
        del self._state[key]
        self._save()
        return True

    def clear(self) -> None:
        self._state.clear()
        self._save()

    def all(self) -> dict[str, Any]:
        return dict(self._state)

    def keys(self) -> list[str]:
        return sorted(self._state)

    def __contains__(self, key: object) -> bool:
        return key in self._state

    def __len__(self) -> int:
        return len(self._state)


def get_xpi_state(path: Path | None = None) -> XpiState:
    """Process-wide XpiState."""
    return XpiState(path)
