#!/usr/bin/env python3
"""
XLI Installer UI — beautiful terminal rendering for dependency installation.

Works through UiPort so it renders correctly in:
- CLI (TerminalUi): Unicode progress bars with colors
- TUI (curses): native progress widgets
- Neovim: via RPC notifications

This module provides enhanced rendering on top of the core DepsInstaller.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xli.ui.port import UiPort
    from xli.core.deps_installer import DependencyInfo


# ==========================================================================
# ANSI COLORS (safe, no dependencies)
# ==========================================================================

class _C:
    """Minimal ANSI color helper. Degrades gracefully if not a TTY."""
    _enabled = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

    @classmethod
    def bold(cls, s: str) -> str:
        return f"\033[1m{s}\033[0m" if cls._enabled else s

    @classmethod
    def green(cls, s: str) -> str:
        return f"\033[32m{s}\033[0m" if cls._enabled else s

    @classmethod
    def cyan(cls, s: str) -> str:
        return f"\033[36m{s}\033[0m" if cls._enabled else s

    @classmethod
    def yellow(cls, s: str) -> str:
        return f"\033[33m{s}\033[0m" if cls._enabled else s

    @classmethod
    def red(cls, s: str) -> str:
        return f"\033[31m{s}\033[0m" if cls._enabled else s

    @classmethod
    def dim(cls, s: str) -> str:
        return f"\033[2m{s}\033[0m" if cls._enabled else s


# ==========================================================================
# GRADIENT LINE
# ==========================================================================

def render_gradient_line(width: int = 60, text: str = "") -> str:
    """A thin gradient rule, optionally with a word in the middle.

    Previously this shelled out to ``rich`` when it happened to be installed
    and fell back to an uncoloured string when it was not — so the dependency
    manager looked different on two machines with the same xli. The gradient
    engine is right here, needs no dependency, and degrades the same way
    everything else does: 24-bit, then 256, then plain.
    """
    from xli.ui.gradient import gradient_rule

    enabled = _C._enabled or bool(os.environ.get("FORCE_COLOR"))
    if not text:
        return gradient_rule(width, enabled=enabled)
    if width <= len(text) + 2:
        return _C.bold(text)
    side = (width - len(text) - 2) // 2
    return (
        gradient_rule(side, enabled=enabled)
        + f" {_C.bold(text)} "
        + gradient_rule(width - side - len(text) - 2, tick=6, enabled=enabled)
    )


def print_gradient_line(width: int = 60, text: str = "") -> None:
    """Print gradient line directly to stdout."""
    print(render_gradient_line(width, text))


#: The braille spinner the TUI cycles through. Kept as a module-level name so
#: callers (and the dependency manager) can advance the same animation.
from xli.tui.palette import SPINNER as SPINNER_FRAMES  # noqa: E402


def render_progress_bar(
    percent: int,
    width: int = 30,
    fill_char: str = "█",
    empty_char: str = "░",
    head_char: str = "▓",
) -> str:
    """Render a Unicode progress bar.

    Example output: ████████████░░░░░░░░░░░░░░░░░░ 40%
    """
    from xli.ui.gradient import ACCENT_GLOW, color_depth, fg

    filled = int(width * percent / 100)
    empty = width - filled

    enabled = _C._enabled or bool(os.environ.get("FORCE_COLOR"))
    if enabled:
        depth = color_depth()
        # The fill runs along the house ramp, so a bar at 90% is visibly the
        # same material as a bar at 10%; the leading edge is the bright end.
        ramp = ACCENT_GLOW.sample(max(filled, 1))
        body = "".join(f"{fg(ramp[index], depth=depth)}{fill_char}" for index, _ in enumerate(range(filled)))
        head = f"{fg(ramp[-1], depth=depth) if filled else ''}{head_char}"
        tail = f"\033[2m{empty_char * empty}\033[0m"
        bar = body + (head if 0 < filled < width else "") + tail
        if filled == width:
            bar = body
    elif filled > 0 and empty > 0:
        bar = fill_char * (filled - 1) + head_char + empty_char * empty
    elif filled == width:
        bar = fill_char * width
    else:
        bar = empty_char * width

    pct_str = f"{percent:3d}%"
    return f"[{bar}] {pct_str}"


def render_stage_header(stage: str, icon: str = "") -> str:
    """Render a stage header line."""
    # Box-drawing, not emoji: an emoji is one codepoint but two cells in some
    # terminals and one in others, which is how a progress line starts
    # overwriting itself.
    icons = {
        "analyze": "◍",
        "describe": "☰",
        "confirm": "?",
        "download": "⇣",
        "install": "✎",
        "verify": "✓",
    }
    stage_icon = icon or icons.get(stage, "·")
    return _C.bold(f"  {stage_icon} {stage.upper()}")


# ==========================================================================
# DEPENDENCY INFO CARD
# ==========================================================================

def render_dep_card(dep: DependencyInfo) -> str:
    """A card for one dependency: what it is, why xli needs it, how to install.

    Built with :func:`xli.ui.gradient.box`, so the frame is straight even when
    a value contains a colour code — the hand-rolled padding here used to count
    escape characters as cells and the right border drifted a few columns out
    for every coloured field.
    """
    from xli.ui.gradient import box

    width = 56
    name = _C.bold("◈ " + dep.name)
    fields = [
        ("Описание", dep.description or "N/A"),
        ("Зачем", dep.why_needed or "нужен ядру xli"),
        ("Кто зовёт", ", ".join(dep.required_by) if dep.required_by else "ядро"),
        ("Установка", f"pip install {dep.install_spec}"),
    ]
    label_width = max(len(field) for field, _ in fields)
    lines = [name, ""]
    for label, value in fields:
        lines.append(f"{_C.cyan(label.ljust(label_width))}  {value}")
    return box(lines, width=width, enabled=_C._enabled)


# ==========================================================================
# INSTALLATION SUMMARY
# ==========================================================================

def render_summary(results: dict[str, bool]) -> str:
    """Render final installation summary with gradient separator."""
    sep = render_gradient_line(50)
    lines = ["", f"  {sep}", _C.bold("  Installation Summary"), f"  {sep}", ""]

    for name, success in results.items():
        if success:
            lines.append(f"    {_C.green('✓')} {name}")
        else:
            lines.append(f"    {_C.red('✗')} {name} {_C.dim('(skipped/failed)')}")

    ok = sum(1 for v in results.values() if v)
    fail = sum(1 for v in results.values() if not v)
    lines.append("")
    lines.append(f"    Total: {_C.green(str(ok))} installed, {_C.red(str(fail))} failed/skipped")
    lines.append(f"  {sep}")
    lines.append("")
    return "\n".join(lines)


# ==========================================================================
# HIGH-LEVEL API
# ==========================================================================

def install_dependencies(
    packages: list[str],
    ui: UiPort,
    registry: dict | None = None,
) -> dict[str, bool]:
    """One-call convenience function: check, describe, confirm, install.

    Uses DepsInstaller internally but enhances output through the UiPort.
    """
    from xli.core.deps_installer import DepsInstaller

    # Show banner with gradient line
    ui.display(
        _C.bold("\n  ◈ XLI Dependency Manager\n") + "  " + render_gradient_line(50) + "\n",
        title=None,
    )

    installer = DepsInstaller(ui=ui, registry=registry)

    # Override progress to use our fancy renderer
    original_progress = ui.progress

    def enhanced_progress(percent: int, message: str = "") -> None:
        bar = render_progress_bar(percent)
        # Extract stage from message if present
        stage = message.split(":")[0] if ":" in message else "working"
        full_line = f"\r  {bar} │ {message}"
        original_progress(percent, full_line)

    ui.progress = enhanced_progress  # type: ignore[method-assign]

    try:
        results = installer.check_and_install(packages)
    finally:
        ui.progress = original_progress  # type: ignore[method-assign]

    # Show summary
    ui.display(render_summary(results))
    return results
