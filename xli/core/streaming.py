#!/usr/bin/env python3
"""
XLI Streaming v4 — Chunks for all UI platforms
"""

import asyncio
from dataclasses import dataclass
from collections.abc import Callable

from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.streaming")


@dataclass
class StreamChunk:
    """Single chunk of streamed content"""
    content: str
    is_final: bool = False
    metadata: dict | None = None


class StreamingHandler:
    """Base streaming handler"""

    def __init__(self):
        self.chunks = []
        self.complete = False
        self._callbacks = []

    def on_chunk(self, callback: Callable[[StreamChunk], None]):
        """Register chunk callback"""
        self._callbacks.append(callback)

    async def emit(self, chunk: StreamChunk):
        """Emit chunk to all callbacks"""
        self.chunks.append(chunk)
        for cb in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(chunk)
                else:
                    cb(chunk)
            except Exception as e:
                logger.log_error("streaming", "Callback failed", exc=e)

    def get_full_text(self) -> str:
        """Get complete streamed text"""
        return "".join(c.content for c in self.chunks)

    def mark_complete(self):
        """Mark streaming as complete"""
        self.complete = True


class TuiStreamingHandler(StreamingHandler):
    """Streaming handler for Textual TUI"""

    def __init__(self, widget=None):
        super().__init__()
        self.widget = widget  # Textual Static widget

    async def emit(self, chunk: StreamChunk):
        await super().emit(chunk)
        if self.widget:
            # Update widget content
            current = self.get_full_text()
            self.widget.update(current[-300:] if len(current) > 300 else current)


class NvimStreamingHandler(StreamingHandler):
    """Streaming handler for Neovim"""

    def __init__(self, nvim=None, bufnr=None):
        super().__init__()
        self.nvim = nvim
        self.bufnr = bufnr

    async def emit(self, chunk: StreamChunk):
        await super().emit(chunk)
        if self.nvim and self.bufnr:
            try:
                buf = self.nvim.buffers[self.bufnr]
                current = buf[:]
                current[-1] = current[-1] + chunk.content if current else chunk.content
                buf[:] = current
            except Exception as e:
                logger.log_error("streaming", "Nvim buffer update failed", exc=e)


class HeadlessStreamingHandler(StreamingHandler):
    """Streaming handler for headless CLI"""

    def __init__(self, prefix: str = ""):
        super().__init__()
        self.prefix = prefix

    async def emit(self, chunk: StreamChunk):
        await super().emit(chunk)
        print(f"{self.prefix}{chunk.content}", end="", flush=True)
        if chunk.is_final:
            print()  # Newline at end

