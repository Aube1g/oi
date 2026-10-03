#!/usr/bin/env python3
"""
XPI Registry v4 — Реестр активных XPI
"""

from dataclasses import dataclass

from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.xpi.registry")


@dataclass
class XpiEntry:
    """Registered XPI entry"""
    name: str
    version: str
    platform: str  # tui, nvim, headless, all
    status: str = "active"  # active, disabled, error


class XpiRegistry:
    """Registry of active XPI plugins"""

    def __init__(self):
        self.entries: dict[str, XpiEntry] = {}
        logger.log_structured("INFO", "xpi.registry", "Registry initialized")

    def register(self, name: str, version: str, platform: str = "all"):
        """Register XPI"""
        self.entries[name] = XpiEntry(name, version, platform)
        logger.log_structured("INFO", "xpi.registry", f"Registered: {name}")

    def unregister(self, name: str):
        """Remove XPI"""
        if name in self.entries:
            del self.entries[name]
            logger.log_structured("INFO", "xpi.registry", f"Unregistered: {name}")

    def list_active(self, platform: str | None = None) -> list[dict]:
        """List active plugins, optionally filtered by platform"""
        result = []
        for entry in self.entries.values():
            if platform and entry.platform not in (platform, "all"):
                continue
            if entry.status == "active":
                result.append({
                    "name": entry.name,
                    "version": entry.version,
                    "platform": entry.platform
                })
        return result

    def get(self, name: str) -> XpiEntry | None:
        """Get entry by name"""
        return self.entries.get(name)
