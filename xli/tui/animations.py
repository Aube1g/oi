#!/usr/bin/env python3
"""
XLI TUI animations — pure functions that produce styled rows for the widget layer.

Every animation is a function of ``tick`` (an integer that advances on each
repaint).  Nothing here touches curses directly; the output is always a list of
``Row`` objects (lists of ``(text, style)`` spans) that ``app.py`` paints.

Design language
---------------
* **Thin lines only** — box-drawing characters like ``─``, ``│``, ``┌``, ``┐``.
  No double-lines, no thick borders.  Thin = elegant.
* **Purple palette** — the accent colour family.  Styles used: ``accent``,
  ``heading``, ``heading2``, ``dim``.
* **Subtle motion** — pulse opacity via style cycling, not character jumping.
  The eye should be guided, not distracted.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xli.tui.palette import Span

# Styles come from the palette, not from widgets: importing widgets here (and
# widgets importing this module back for `animated_separator`) was the import
# cycle `xli graph` kept reporting. The palette has no dependencies at all.
from xli.tui.palette import (  # noqa: E402
    ACCENT,
    BOLD,
    DIM,
    HEADING,
    HEADING2,
    NORMAL,
    SPINNER,
    Span,
    span,
)

# ── Box-drawing primitives (thin) ────────────────────────────────────────────
H = "─"          # horizontal
V = "│"          # vertical
TL = "┌"         # top-left
TR = "┐"         # top-right
BL = "└"         # bottom-left
BR = "┘"         # bottom-right
T_DOWN = "┬"     # tee pointing down
T_UP = "┴"       # tee pointing up
CROSS = "┼"      # cross
DOT = "·"        # subtle dot
BULLET = "◈"     # purple diamond bullet

# ── Spinner variants ────────────────────────────────────────────────────────
#: A thin-line spinner that looks like a rotating pipe segment.
PIPE_SPINNER = ("│", "╱", "─", "╲")

#: Dots that grow and shrink — good for "thinking" states.
PULSE_DOTS = ("⠁", "⠂", "⠄", "⡀", "⢀", "⠠", "⠐", "⠈")

#: Orbiting dots for longer operations.
ORBIT = ("⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷")


def spinner_frame(tick: int, *, variant: str = "braille") -> str:
    """Return one spinner character for the given tick."""
    if variant == "pipe":
        return PIPE_SPINNER[tick % len(PIPE_SPINNER)]
    if variant == "orbit":
        return ORBIT[tick % len(ORBIT)]
    if variant == "pulse":
        return PULSE_DOTS[tick % len(PULSE_DOTS)]
    # default braille — the same frames the status bar uses
    from xli.tui.palette import SPINNER

    return SPINNER[tick % len(SPINNER)]


# ── Style pulsing ───────────────────────────────────────────────────────────
#: Cycle through styles to create a breathing / pulsing effect.
_PULSE_STYLES = (DIM, ACCENT, HEADING, ACCENT)


def pulse_style(tick: int, *, speed: int = 4) -> str:
    """Return a style name that cycles dim → accent → heading → accent.

    ``speed`` controls how many ticks each phase lasts.  Higher = slower.
    """
    phase = (tick // max(1, speed)) % len(_PULSE_STYLES)
    return _PULSE_STYLES[phase]


def wave_style(tick: int, offset: int = 0, *, period: int = 16) -> str:
    """Sine-wave brightness mapped to styles.

    Each character can call this with its own ``offset`` to create a travelling
    wave across a line of text.
    """
    t = ((tick + offset) % period) / period * 2 * math.pi
    brightness = (math.sin(t) + 1) / 2  # 0.0 … 1.0
    if brightness < 0.3:
        return DIM
    if brightness < 0.7:
        return ACCENT
    return HEADING


# ── Thin-line drawing helpers ───────────────────────────────────────────────
def thin_hline(width: int, *, style: str = ACCENT) -> list[Span]:
    """A horizontal thin line spanning ``width`` cells."""
    if width <= 0:
        return []
    return [span(H * width, style)]


def thin_border_top(width: int, *, style: str = ACCENT) -> list[Span]:
    """``┌────────────────┐`` at the given width (including corners)."""
    if width <= 2:
        return [span(TL + TR, style)]
    return [span(TL + H * (width - 2) + TR, style)]


def thin_border_bottom(width: int, *, style: str = ACCENT) -> list[Span]:
    """``└────────────────┘`` at the given width."""
    if width <= 2:
        return [span(BL + BR, style)]
    return [span(BL + H * (width - 2) + BR, style)]


def thin_border_row(width: int, inner: list[Span], *, style: str = ACCENT) -> list[Span]:
    """Wrap ``inner`` spans with thin vertical bars: ``│ content │``."""
    from xli.ui.text import display_width
    used = sum(display_width(s[0]) for s in inner)
    fill = max(0, width - 2 - used)
    result: list[Span] = [span(V, style)]
    result.extend(inner)
    if fill > 0:
        result.append(span(" " * fill, NORMAL))
    result.append(span(V, style))
    return result


def thin_box(rows: list[list[Span]], width: int, *, style: str = ACCENT) -> list[list[Span]]:
    """Wrap multiple rows in a thin-line box."""
    boxed: list[list[Span]] = [thin_border_top(width, style=style)]
    for row in rows:
        boxed.append(thin_border_row(width, row, style=style))
    boxed.append(thin_border_bottom(width, style=style))
    return boxed


# ── Animated separator ──────────────────────────────────────────────────────
def animated_separator(width: int, tick: int, *, style: str = ACCENT) -> list[Span]:
    """A thin horizontal line with a travelling bright spot.

    The spot moves left-to-right using a sine offset, creating a scanning
    effect without any character movement — only the style changes.
    """
    if width <= 0:
        return []
    chars: list[Span] = []
    # Position of the bright spot (travels across the line)
    spot = int((math.sin(tick * 0.15) + 1) / 2 * (width - 1))
    for i in range(width):
        dist = abs(i - spot)
        if dist == 0:
            chars.append(span(H, HEADING))
        elif dist <= 2:
            chars.append(span(H, ACCENT))
        else:
            chars.append(span(H, style))
    return chars


# ── Progress bar ────────────────────────────────────────────────────────────
def progress_bar(
    fraction: float,
    width: int,
    tick: int,
    *,
    label: str = "",
    show_percent: bool = True,
) -> list[Span]:
    """A thin-line progress bar: ``┤████████░░░░░░├ 65%``.

    Uses box-drawing characters for the frame and filled/empty blocks.
    The leading edge pulses between ACCENT and HEADING.
    """
    fraction = max(0.0, min(1.0, fraction))

    # Reserve space for borders and optional label/percent
    suffix_parts: list[str] = []
    if show_percent:
        suffix_parts.append(f" {int(fraction * 100)}%")
    if label:
        suffix_parts.append(f" {label}")
    suffix = "".join(suffix_parts)

    bar_width = max(4, width - 2 - len(suffix))  # 2 for the end caps
    filled = int(bar_width * fraction)
    empty = bar_width - filled

    parts: list[Span] = [span("┤", DIM)]
    if filled > 0:
        parts.append(span("█" * filled, ACCENT))
    # Pulse the leading edge
    if empty > 0 and filled < bar_width:
        edge_style = pulse_style(tick, speed=3)
        parts.append(span("░", edge_style))
        if empty > 1:
            parts.append(span("░" * (empty - 1), DIM))
    parts.append(span("├", DIM))
    if suffix:
        parts.append(span(suffix, DIM))
    return parts


# ── Typing reveal ───────────────────────────────────────────────────────────
def typing_reveal(text: str, tick: int, *, chars_per_tick: int = 2) -> str:
    """Reveal ``text`` progressively based on tick count.

    Useful for animating assistant messages appearing character by character.
    """
    visible = min(len(text), tick * chars_per_tick)
    return text[:visible]


# ── Startup splash ──────────────────────────────────────────────────────────
_SPLASH_LINES = [
    " ◈ XLI",
    "   autonomous coding agent",
]


def splash_rows(width: int, tick: int) -> list[list[Span]]:
    """Animated startup splash screen with fade-in and wave effects.

    Returns a list of rows centred vertically (caller decides placement).
    """
    rows: list[list[Span]] = []
    for line_idx, line in enumerate(_SPLASH_LINES):
        # Each line fades in after the previous one
        fade_start = line_idx * 8
        if tick < fade_start:
            continue
        local_tick = tick - fade_start
        alpha = min(1.0, local_tick / 8.0)

        spans: list[Span] = []
        for char_idx, ch in enumerate(line):
            if alpha < 0.3:
                style = DIM
            elif line_idx == 0 and ch == "◈":
                style = pulse_style(tick, speed=3)
            else:
                style = wave_style(tick, offset=char_idx * 2)
            spans.append(span(ch, style))

        # Centre the row
        from xli.ui.text import display_width
        text_width = sum(display_width(s[0]) for s in spans)
        pad = max(0, (width - text_width) // 2)
        row: list[Span] = []
        if pad > 0:
            row.append(span(" " * pad, NORMAL))
        row.extend(spans)
        rows.append(row)

    # Add a thin animated line below the splash
    if tick > len(_SPLASH_LINES) * 8:
        line_width = min(width - 4, 40)
        pad = max(0, (width - line_width) // 2)
        sep: list[Span] = []
        if pad > 0:
            sep.append(span(" " * pad, NORMAL))
        sep.extend(animated_separator(line_width, tick))
        rows.append(sep)

    return rows


def splash_duration() -> int:
    """Number of ticks the splash animation needs to fully complete."""
    return len(_SPLASH_LINES) * 8 + 20
