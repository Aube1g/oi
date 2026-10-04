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
import re
import shutil
import sys
import time
from typing import Any, Callable

#: Used only to measure a coloured line; the escapes take no cells.
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

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
#: Phase names are interface text, so they are Russian. The keys stay English
#: because they are also protocol (`build()` emits them).
_PHASE_WORD = {
    "preflight": "проверка окружения",
    "cythonize": "подготовка исходников",
    "compile": "компиляция",
    "link": "сборка связи",
    "done": "готово",
}


#: Short forms for when the full timeline does not fit the box.
_PHASE_SHORT = {
    "preflight": "проверка",
    "cythonize": "исходники",
    "compile": "сборка",
    "link": "связь",
    "done": "готово",
}


def _phase_word(phase: str, short: bool = False) -> str:
    from xli.ui.locale import t

    if short:
        return _PHASE_SHORT.get(phase, phase)
    word = t(f"kernel_phase_{phase}")
    return word if word != f"kernel_phase_{phase}" else _PHASE_WORD.get(phase, phase)


def _seconds(value: float) -> str:
    from xli.ui.locale import seconds_word

    return seconds_word(value)


def _size(size: int) -> str:
    """`412 КиБ` — the unit a build output is actually measured in."""
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f}".replace(".", ",") + " МиБ"
    if size >= 1024:
        return f"{size / 1024:.0f} КиБ"
    return f"{size} Б"


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
        self._module_started = time.monotonic()
        self._durations: dict[str, float] = {}
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
            self._module_started = time.monotonic()
        if "module_done" in event:
            name = event["module_done"]
            self._done_modules.add(name)
            self._durations[name] = time.monotonic() - self._module_started
            self._current_module = ""
        if "module_fail" in event:
            self._failed_modules[event["module_fail"]] = event.get("error", "unknown")
            self._done_modules.add(event["module_fail"])
            self._current_module = ""

        self._tick += 1
        self._render()

    def finish(self, report: Any) -> None:
        """Render the final summary after the build completes."""
        from xli.ui.locale import plural, t

        if not self._isatty:
            self._out.write(f"\n{report.summary()}\n")
            self._out.flush()
            return

        self._phase = "done"
        self._tick += 1
        if self._modules:
            # One last frame with everything ticked; skipped when the build
            # never got as far as a module list (a failed preflight), because
            # then it would paint a "scanning…" row over the report.
            self._render()

        # Move below the animation block
        self._out.write("\n")
        elapsed = time.monotonic() - self._start_time

        w = self._width
        lines: list[str] = []
        lines.append(self._top_border(w))

        if report.ok:
            if report.built:
                modules_word = plural(len(report.built), "модуль", "модуля", "модулей")
                headline = t(
                    "kernel_built",
                    count=len(report.built),
                    modules=modules_word,
                    seconds=_seconds(report.seconds),
                )
            else:
                headline = t("kernel_no_changes")
            title = f"{_PURPLE_BRIGHT}{_BOLD}◈{_RESET} {headline}"
            lines.append(f"{V} {self._fit(title, w - 3)}{V}")
        else:
            title = f"{_RED}{_BOLD}✗{_RESET} " + t("kernel_failed", count=len(report.failed))
            lines.append(f"{V} {self._fit(title, w - 3)}{V}")

        lines.append(self._separator(w))

        if report.artifacts:
            total = _size(report.bytes_built)
            files = plural(
                len(report.artifacts),
                t("kernel_file_word_one"),
                t("kernel_file_word_few"),
                t("kernel_file_word_many"),
            )
            # The mark is a wide glyph in some fonts, so the width goes to
            # `_fit` and the row is assembled like every other row.
            content = f"{_GREEN}◆{_RESET} " + t(
                "kernel_artifacts", count=len(report.artifacts), files=files, size=total
            )
            lines.append(f"{V} {self._fit(content, w - 3)}{V}")
        if report.skipped:
            skip_str = ", ".join(report.skipped)
            content = f"{_GRAY}◇ {t('kernel_skipped_list')}{_RESET} {skip_str}"
            lines.append(f"{V} {self._fit(content, w - 3)}{V}")
        for fail in report.failed:
            mod = fail.get("module", "?")
            err = fail.get("error", "")
            content = f"{_RED}✗{_RESET} {t('kernel_failed_one', module=mod, error=err)}"
            lines.append(f"{V} {self._fit(content, w - 3)}{V}")

        # Where the time went: the same numbers as a bar chart, because
        # "which module is slow" is a comparison, and text makes you do it.
        slowest = report.slowest(5) if hasattr(report, "slowest") else []
        if slowest and report.ok:
            lines.append(self._separator(w))
            from xli.ui import charts

            header = f"{_PURPLE_DIM}{t('kernel_slowest')}{_RESET}"
            lines.append(f"{V} {self._fit(header, w - 3)}{V}")
            for line in charts.bars(slowest, width=max(12, w - 34), show_value=True):
                lines.append(f"{V} {self._fit(line, w - 3)}{V}")

        warnings_count = (report.log or "").lower().count("warning")
        if warnings_count:
            content = f"{_YELLOW}!{_RESET} {t('kernel_warnings', count=warnings_count)}"
            lines.append(f"{V} {self._fit(content, w - 3)}{V}")

        lines.append(self._bottom_border(w))

        output = "\n".join(lines) + "\n"
        self._out.write(output)
        self._out.write(_SHOW_CURSOR)
        self._out.flush()

    # ── rendering internals ──────────────────────────────────────────────
    def _render(self) -> None:
        """Redraw the animation block in-place.

        Every row goes through `_fit`, so the frame is exactly `w` cells wide
        no matter what is inside it. The previous version padded with `:<{w}`
        on strings that contained colour escapes, which counts escape bytes as
        visible characters and tore the right border.
        """
        from xli.ui.locale import t

        w = self._width
        lines: list[str] = []

        # Erase previous frame
        if self._lines_drawn > 0:
            for _ in range(self._lines_drawn):
                lines.append(f"{_MOVE_UP}{_CLEAR_LINE}")

        # Header: what is running, since when
        lines.append(self._top_border(w))
        spinner_ch = _SPINNER[self._tick % len(_SPINNER)]
        elapsed = time.monotonic() - self._start_time
        header = (
            f"{_PURPLE_BRIGHT}{spinner_ch}{_RESET} {_BOLD}{t('kernel_title')}{_RESET}"
            f" {_PURPLE_DIM}·{_RESET} {_PURPLE_BRIGHT}{_phase_word(self._phase)}{_RESET}"
            f" {_PURPLE_DIM}· {_seconds(elapsed)}{_RESET}"
        )
        lines.append(f"{V} {self._fit(header, w - 3)}{V}")

        lines.append(f"{V} {self._phase_line(w)}{V}")
        lines.append(self._separator(w))

        # Module rows
        total = len(self._modules) if self._modules else 1
        done_count = len(self._done_modules)

        for idx, mod_name in enumerate(self._modules or ["сканирую..."], start=0):
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

            if is_done:
                frac = 1.0
            elif is_current:
                # A partial, breathing fill: the exact fraction is unknown
                # until the compiler returns, and a fake percentage would be a
                # lie. This says "working", not "40% done".
                frac = 0.3 + 0.4 * ((math.sin(self._tick * 0.3 + idx) + 1) / 2)
            else:
                frac = 0.0

            timing = ""
            if is_failed:
                timing = "ошибка"
            elif is_done and mod_name in self._durations:
                timing = _seconds(self._durations[mod_name])
            elif is_current:
                timing = _seconds(time.monotonic() - self._module_started)

            name_display = mod_name[:14].ljust(14)
            # mark(2) + space + name(14) + space + bar + space + timing(8)
            bar_width = max(8, w - 3 - 26 - 8)
            bar = self._progress_bar(frac, bar_width, self._tick + idx)
            row = f"{mark} {name_display} {bar} {_PURPLE_DIM}{timing:>8}{_RESET}"
            lines.append(f"{V} {self._fit(row, w - 3)}{V}")

        # Overall progress, with the count it is made of
        lines.append(self._separator(w))
        count = t("kernel_modules_done", done=done_count, total=total)
        overall = done_count / total if total else 0.0
        overall_bar = self._progress_bar(overall, max(10, w - 3 - 8 - len(count) - 2), self._tick)
        pct = f"{int(overall * 100):>3}%"
        lines.append(
            f"{V} {self._fit(f'{overall_bar} {pct} {_PURPLE_DIM}{count}{_RESET}', w - 3)}{V}"
        )

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

    def _fit(self, text: str, width: int) -> str:
        """Pad or trim a coloured string to `width` visible cells.

        Colour codes occupy no cells, so `f"{text:<{width}}"` on a coloured
        string pads by bytes and the right border ends up in the wrong column.
        Visible width is what has to match, and a truncation ends in `…` with
        the colour reset restored so the border keeps its own shade.
        """
        from xli.ui.text import display_width, truncate

        plain = _ANSI_RE.sub("", text)
        visible = display_width(plain)
        if visible > width:
            # Truncating drops the colour, which is the honest trade: a half-cut
            # escape sequence would corrupt the rest of the line.
            text = truncate(plain, width)
            visible = display_width(text)
        return text + " " * max(0, width - visible)

    def _phase_line(self, w: int) -> str:
        """`✓ проверка окружения › ⣽ компиляция › ○ сборка связи` — the stages."""
        index = _PHASES.index(self._phase) if self._phase in _PHASES else 0
        from xli.tui.frame import display_width

        # Long words first; if the line does not fit, use the short ones
        # rather than cutting a stage name in half.
        for short in (False, True):
            parts: list[str] = []
            for position, phase in enumerate(_PHASES[:-1]):
                word = _phase_word(phase, short=short)
                if position < index:
                    parts.append(f"{_GREEN}✓{_RESET} {_PURPLE_DIM}{word}{_RESET}")
                elif position == index:
                    spinner = _SPINNER[self._tick % len(_SPINNER)]
                    parts.append(f"{_PURPLE_BRIGHT}{spinner} {word}{_RESET}")
                else:
                    parts.append(f"{_PURPLE_DIM}○ {word}{_RESET}")
            line = f" {_PURPLE_DIM}›{_RESET} ".join(parts)
            if display_width(line) <= w - 3:
                return self._fit(line, w - 3)
        return self._fit(line, w - 3)

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
