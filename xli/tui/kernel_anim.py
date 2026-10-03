#!/usr/bin/env python3
"""
XLI Kernel Build Animation — beautiful ANSI terminal UI for ``xli kernel build``.

Replaces the plain Cython output with a custom animated display:
* thin purple box-drawing borders (``┌─┐│└┘``)
* per-module progress bars with a pulsing leading edge
* phase spinner (preflight → cythonize → compile → link → done)
* all colours in the purple family via ANSI 256-colour codes

Usage::

    from xli.tui.kernel_anim import KernelBuildAnimator
    from xli.manager.kernel_build import build

    animator = KernelBuildAnimator()
    report = build(on_progress=animator.on_progress)
    animator.finish(report)
"""

from __future__ import annotations

import math
import os
import shutil
import sys
import time
from typing import Any, Callable

# ── ANSI colour palette (purple family) ─────────────────────────────────────
# Using 256-colour codes for consistent rendering across terminals.
_RESET = "\033[0m"
_HIDE_CURSOR = "\033[?25l"
_SHOW_CURSOR = "\033[?25h"
_CLEAR_LINE = "\033[2K"
_MOVE_UP = "\033[1A"

# Purple shades (256-colour)
_PURPLE_BRIGHT = "\033[38;5;141m"   # light purple / violet
_PURPLE_MID = "\033[38;5;99m"      # medium purple
_PURPLE_DIM = "\033[38;5;60m"      # dark muted purple
_WHITE = "\033[38;5;255m"
_GRAY = "\033[38;5;240m"
_GREEN = "\033[38;5;114m"
_RED = "\033[38;5;167m"
_YELLOW = "\033[38;5;179m"
_BOLD = "\033[1m"

# ── Box-drawing characters (thin) ───────────────────────────────────────────
H = "─"
V = "│"
TL = "┌"
TR = "┐"
BL = "└"
BR = "┘"

# ── Spinner frames ──────────────────────────────────────────────────────────
_SPINNER = ("⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷")
_PHASES = ("preflight", "cythonize", "compile", "link", "done")


def _term_width() -> int:
    """Current terminal width, with a sensible fallback."""
    try:
        return shutil.get_terminal_size((80, 24)).columns
    except Exception:  # noqa: BLE001
        return 80


class KernelBuildAnimator:
    """Drives an animated kernel-build display on a raw ANSI terminal.

    Call :meth:`on_progress` as a callback from
    :func:`xli.manager.kernel_build.build`.  When the build finishes, call
    :meth:`finish` to render the summary.
    """

    def __init__(self, *, stream: Any = None) -> None:
        self._out = stream or sys.stderr
        self._width = max(40, min(_term_width(), 100))
        self._tick = 0
        self._phase = "preflight"
        self._modules: list[str] = []
        self._done_modules: set[str] = set()
        self._failed_modules: dict[str, str] = {}
        self._current_module: str = ""
        self._start_time = time.monotonic()
        self._lines_drawn = 0
        self._isatty = hasattr(self._out, "isatty") and self._out.isatty()

    # ── public API ───────────────────────────────────────────────────────
    def on_progress(self, event: dict[str, Any]) -> None:
        """Progress callback — pass as ``on_progress=`` to ``build()``.

        Recognised events::

            {"phase": "cythonize"}
            {"module_start": "parser"}
            {"module_done": "parser"}
            {"module_fail": "parser", "error": "..."}
            {"total_modules": 12}
        """
        if not self._isatty:
            # Non-interactive: just print one-liners
            self._plain_log(event)
            return

        if "phase" in event:
            self._phase = event["phase"]
        if "total_modules" in event:
            self._modules = [f"module_{i}" for i in range(event["total_modules"])]
        if "module_names" in event:
            self._modules = list(event["module_names"])
        if "module_start" in event:
            self._current_module = event["module_start"]
        if "module_done" in event:
            self._done_modules.add(event["module_done"])
            self._current_module = ""
        if "module_fail" in event:
            self._failed_modules[event["module_fail"]] = event.get("error", "unknown")
            self._done_modules.add(event["module_fail"])
            self._current_module = ""

        self._tick += 1
        self._render()

    def finish(self, report: Any) -> None:
        """Render the final summary after the build completes."""
        if not self._isatty:
            self._out.write(f"\n{report.summary()}\n")
            self._out.flush()
            return

        self._phase = "done"
        self._tick += 1
        self._render()

        # Move below the animation block
        self._out.write("\n")
        elapsed = time.monotonic() - self._start_time

        w = self._width
        lines: list[str] = []
        lines.append(self._top_border(w))

        if report.ok:
            title = f" {_PURPLE_BRIGHT}{_BOLD}◈{_RESET} kernel compiled in {elapsed:.1f}s"
            lines.append(f"{V}{title:<{w - 1}}{V}")
        else:
            title = f" {_RED}{_BOLD}✗{_RESET} build failed ({len(report.failed)} error(s))"
            lines.append(f"{V}{title:<{w - 1}}{V}")

        lines.append(self._separator(w))

        if report.built:
            built_str = ", ".join(report.built)
            lines.append(f"{V} {_GREEN}built:{_RESET}   {built_str:<{w - 12}}{V}")
        if report.skipped:
            skip_str = ", ".join(report.skipped)
            lines.append(f"{V} {_GRAY}skipped:{_RESET} {skip_str:<{w - 12}}{V}")
        for fail in report.failed:
            mod = fail.get("module", "?")
            err = fail.get("error", "")[:w - 20]
            lines.append(f"{V} {_RED}FAIL:{_RESET}    {mod}: {err:<{w - 16}}{V}")

        lines.append(self._bottom_border(w))

        output = "\n".join(lines) + "\n"
        self._out.write(output)
        self._out.write(_SHOW_CURSOR)
        self._out.flush()

    # ── rendering internals ──────────────────────────────────────────────
    def _render(self) -> None:
        """Redraw the animation block in-place."""
        w = self._width
        lines: list[str] = []

        # Erase previous frame
        if self._lines_drawn > 0:
            for _ in range(self._lines_drawn):
                lines.append(f"{_MOVE_UP}{_CLEAR_LINE}")

        # Header
        lines.append(self._top_border(w))
        spinner_ch = _SPINNER[self._tick % len(_SPINNER)]
        phase_idx = _PHASES.index(self._phase) if self._phase in _PHASES else 0
        header = f" {_PURPLE_BRIGHT}{spinner_ch}{_RESET} XLI Kernel Build {_PURPLE_DIM}·{_RESET} {self._phase}"
        lines.append(f"{V}{header:<{w - 1}}{V}")
        lines.append(self._separator(w))

        # Module rows
        total = len(self._modules) if self._modules else 1
        done_count = len(self._done_modules)

        for idx, mod_name in enumerate(self._modules or ["scanning..."]):
            is_done = mod_name in self._done_modules
            is_failed = mod_name in self._failed_modules
            is_current = mod_name == self._current_module

            if is_failed:
                mark = f"{_RED}✗{_RESET}"
            elif is_done:
                mark = f"{_GREEN}✓{_RESET}"
            elif is_current:
                mark = f"{_PURPLE_BRIGHT}{_SPINNER[(self._tick + idx) % len(_SPINNER)]}{_RESET}"
            else:
                mark = f"{_PURPLE_DIM}○{_RESET}"

            # Progress bar for this module
            bar_w = max(10, w - 30)
            if is_done:
                frac = 1.0
            elif is_current:
                # Animate partial fill
                frac = 0.3 + 0.4 * ((math.sin(self._tick * 0.3 + idx) + 1) / 2)
            else:
                frac = 0.0

            bar = self._progress_bar(frac, bar_w, self._tick + idx)
            name_display = mod_name[:14].ljust(14)
            lines.append(f"{V} {mark} {name_display} {bar}{V}")

        # Overall progress
        lines.append(self._separator(w))
        overall = done_count / total if total else 0
        overall_bar = self._progress_bar(overall, w - 6, self._tick)
        pct = f"{int(overall * 100):>3}%"
        lines.append(f"{V} {overall_bar} {pct}{V}")

        lines.append(self._bottom_border(w))

        output = "\n".join(lines) + "\n"
        self._out.write(_HIDE_CURSOR + output)
        self._out.flush()
        self._lines_drawn = len(lines)

    def _progress_bar(self, fraction: float, width: int, tick: int) -> str:
        """Thin-line progress bar: ``┤████░░░░├``."""
        fraction = max(0.0, min(1.0, fraction))
        filled = int(width * fraction)
        empty = width - filled

        parts: list[str] = [f"{_PURPLE_DIM}┤{_RESET}"]
        if filled > 0:
            parts.append(f"{_PURPLE_MID}{'█' * filled}{_RESET}")
        if empty > 0:
            # Pulse the leading edge
            if filled < width:
                pulse = (math.sin(tick * 0.5) + 1) / 2
                if pulse > 0.5:
                    edge_color = _PURPLE_BRIGHT
                else:
                    edge_color = _PURPLE_MID
                parts.append(f"{edge_color}░{_RESET}")
                if empty > 1:
                    parts.append(f"{_PURPLE_DIM}{'░' * (empty - 1)}{_RESET}")
            else:
                parts.append(f"{_PURPLE_DIM}{'░' * empty}{_RESET}")
        parts.append(f"{_PURPLE_DIM}├{_RESET}")
        return "".join(parts)

    # ── border helpers ───────────────────────────────────────────────────
    def _top_border(self, w: int) -> str:
        inner = w - 2
        # Animated travelling highlight
        spot = int((math.sin(self._tick * 0.12) + 1) / 2 * max(0, inner - 1))
        chars: list[str] = []
        for i in range(inner):
            dist = abs(i - spot)
            if dist == 0:
                chars.append(f"{_PURPLE_BRIGHT}{H}{_RESET}")
            elif dist <= 2:
                chars.append(f"{_PURPLE_MID}{H}{_RESET}")
            else:
                chars.append(f"{_PURPLE_DIM}{H}{_RESET}")
        return f"{_PURPLE_DIM}{TL}{_RESET}{''.join(chars)}{_PURPLE_DIM}{TR}{_RESET}"

    def _bottom_border(self, w: int) -> str:
        inner = w - 2
        spot = int((math.cos(self._tick * 0.12) + 1) / 2 * max(0, inner - 1))
        chars: list[str] = []
        for i in range(inner):
            dist = abs(i - spot)
            if dist == 0:
                chars.append(f"{_PURPLE_BRIGHT}{H}{_RESET}")
            elif dist <= 2:
                chars.append(f"{_PURPLE_MID}{H}{_RESET}")
            else:
                chars.append(f"{_PURPLE_DIM}{H}{_RESET}")
        return f"{_PURPLE_DIM}{BL}{_RESET}{''.join(chars)}{_PURPLE_DIM}{BR}{_RESET}"

    def _separator(self, w: int) -> str:
        inner = w - 2
        spot = int((math.sin(self._tick * 0.15 + 1.5) + 1) / 2 * max(0, inner - 1))
        chars: list[str] = []
        for i in range(inner):
            dist = abs(i - spot)
            if dist == 0:
                chars.append(f"{_PURPLE_BRIGHT}{H}{_RESET}")
            elif dist <= 3:
                chars.append(f"{_PURPLE_MID}{H}{_RESET}")
            else:
                chars.append(f"{_PURPLE_DIM}{H}{_RESET}")
        return f"{_PURPLE_DIM}├{_RESET}{''.join(chars)}{_PURPLE_DIM}┤{_RESET}"

    def _plain_log(self, event: dict[str, Any]) -> None:
        """Fallback for non-TTY streams."""
        if "phase" in event:
            self._out.write(f"[{event['phase']}]\n")
        if "module_done" in event:
            self._out.write(f"  ✓ {event['module_done']}\n")
        if "module_fail" in event:
            self._out.write(f"  ✗ {event['module_fail']}: {event.get('error', '')}\n")
        self._out.flush()


def run_animated_build(
    targets: list[str] | None = None,
    *,
    force: bool = False,
    jobs: int | None = None,
    verbose: bool = False,
) -> int:
    """Run ``kernel_build.build()`` with the animated display.

    Returns 0 on success, 1 on failure.
    """
    from xli.manager.kernel_build import build

    animator = KernelBuildAnimator()
    report = build(
        targets,
        force=force,
        jobs=jobs,
        verbose=verbose,
        on_progress=animator.on_progress,
    )
    animator.finish(report)
    return 0 if report.ok else 1
