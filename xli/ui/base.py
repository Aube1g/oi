#!/usr/bin/env python3
"""
XLI UI Base v4 — Adapter interface for all UI platforms
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.ui.base")




@dataclass
class UIEvent:
    """UI event for cross-platform communication"""
    type: str
    data: dict


class UIState:
    """Shared UI state"""
    def __init__(self):
        self._state = {}

    def get(self, key, default=None):
        return self._state.get(key, default)

    def set(self, key, value):
        self._state[key] = value


class XliUI(ABC):
    """Abstract base for all UI implementations"""

    def __init__(self):
        self.logger = StructuredLogger("xli.ui")

    @abstractmethod
    def display(self, text: str, title: str | None = None):
        """Display text output"""
        pass

    @abstractmethod
    async def input(self, prompt: str, default: str = "") -> str:
        """Get user input"""
        pass

    @abstractmethod
    def notify(self, msg: str, level: str = "info"):
        """Show notification"""
        pass

    @abstractmethod
    def open_buffer(self, content: list[str], name: str = "XLI",
                    filetype: str = "markdown"):
        """Open content buffer"""
        pass

    @abstractmethod
    def progress(self, percent: int, message: str = ""):
        """Show progress"""
        pass

    @abstractmethod
    def clear(self):
        """Clear display"""
        pass

    @abstractmethod
    def choice(self, question: str, options: list[str],
               default: int | None = None) -> int:
        """Show choice dialog"""
        pass

    @abstractmethod
    def confirm(self, question: str) -> bool:
        """Show yes/no dialog"""
        pass

    def display_result(self, result: dict[str, Any]):
        """Display chain result"""
        self.display("=" * 60, "FIRE XLI PRO v4 — Result")

        if result.get("plan"):
            self.display(result["plan"][:500], "PLAN")

        if result.get("coder"):
            self.display(result["coder"][:800], "CODER")

        if result.get("debugger"):
            self.display(result["debugger"][:500], "DEBUGGER")

        if result.get("tester"):
            self.display(result["tester"][:500], "TESTER")

        if result.get("optimizer"):
            self.display(result["optimizer"][:500], "OPTIMIZER")

        if result.get("reviewer"):
            self.display(result["reviewer"][:500], "REVIEWER")

        status = "✅ SUCCESS" if result.get("success") else "❌ FAILED"
        self.display(f"\n{status}")
        self.display("=" * 60)

