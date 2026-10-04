#!/usr/bin/env python3
"""XLI ANSI — markdown as coloured terminal text for the line-based front ends.

The CLI and the REPL both print assistant answers into a plain stream, and
this is the one renderer they share: it uses the same markdown parser as the
curses TUI, so a heading or a fence reads the same in all three.

Two things changed when the gradient engine landed:

* **Headings glow.** ``heading`` is no longer one violet code — the text is
  painted per character along the house ramp, and passing ``tick`` shifts the
  ramp so the glow travels. That is the "animation" the CLI can afford: one
  already-rendered string, no cursor movement.
* **Code is tokenised.** Keywords, strings, numbers and comments each get a
  colour, because a fence is usually most of an answer.

Colour degrades to plain text when stdout is not a terminal or ``NO_COLOR`` is
set — a piped answer must stay machine-readable. Depth is automatic: 24-bit,
then xterm-256, then the 16 base colours.
"""

from __future__ import annotations

import os
import sys

from xli.ui.gradient import (
    BAD_GLOW,
    GOOD_GLOW,
    WARN_GLOW,
    color_depth,
    fg,
    gradient_rule,
    style_colors,
)

# Markdown span styles, mapped to the same vocabulary the TUI's curses palette
# uses, so one renderer drives both front ends.
ANSI_FOR_STYLE: dict[str, str | None] = {
    "normal": None,
    "frame": "38;5;97",
    "gauge": "38;5;141",
    "dim": "2",
    "bold": "1",
    # Violet, not cyan: cyan reads as "info / link", violet is this app's own
    # accent. Flat colour is the fallback; glowing styles are painted per
    # character below.
    "accent": "35",
    "good": "32",
    "warn": "33",
    "bad": "31",
    "heading": "1;95",
    "heading2": "1;35",
    "heading3": "1",
    "code": "7",
    "quote": "3;90",
    "link": "4;35",
    "italic": "3",
    "strike": "9",
    # `==выделение==`: a highlighter stroke. Reverse video is the closest a
    # terminal gets to one, and it survives every colour scheme.
    "mark": "7",
    # Syntax highlighting inside fences.
    "kw": "1;35",
    "str": "32",
    "num": "33",
    "com": "2;3",
    "fn": "36",
    "op": "2",
}

#: Styles that get the travelling gradient instead of a flat code.
GLOWING = frozenset({"heading", "heading2", "heading3", "rule"})

#: Styles that render as a full-width accent-coloured bar.
RULE_STYLE = "rule"


def ansi_enabled() -> bool:
    """FORCE_COLOR wins over NO_COLOR; otherwise trust the tty."""
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def terminal_width(default: int = 80) -> int:
    """Usable width, from COLUMNS or the tty, never wider than the terminal."""
    raw = os.environ.get("COLUMNS")
    if raw and raw.isdigit() and int(raw) > 0:
        return int(raw)
    try:
        return max(20, os.get_terminal_size(sys.stdout.fileno()).columns)
    except (OSError, ValueError, AttributeError):
        return default


def paint_span(text: str, style: str, *, enabled: bool, tick: int = 0) -> str:
    """One span as ANSI, honouring the glow styles.

    Public because other line-based front ends (the REPL's prompt, the nvim
    bridge's preview) want the same colour for the same word.
    """
    if not text:
        return ""
    if not enabled:
        return text
    if style.startswith("#") and len(style) == 7:
        # A `#RRGGBB` style is how the agent palette hands out identity
        # colours: the painters resolve it, the layout never sees it.
        from xli.ui.gradient import as_rgb

        rgb = as_rgb(style)
        if rgb is not None:
            depth = color_depth()
            return f"{fg(rgb, depth=depth)}{text}\033[0m"
    if style in GLOWING:
        colors = style_colors(style, len(text), tick=tick)
        if colors is not None:
            depth = color_depth()
            prefix = "\033[1m"
            return "".join(
                f"{prefix if index == 0 else ''}{fg(color, depth=depth)}{char}"
                for index, (char, color) in enumerate(zip(text, colors, strict=False))
            ) + "\033[0m"
    code = ANSI_FOR_STYLE.get(style)
    return f"\033[{code}m{text}\033[0m" if code else text


def render_markdown_ansi(
    markdown: str,
    width: int | None = None,
    *,
    enabled: bool | None = None,
    tick: int = 0,
) -> str:
    """Markdown as ANSI-coloured text.

    ``enabled`` lets a caller with its own colour decision (the CLI's STYLE)
    keep it; None means decide from the environment. ``tick`` animates the
    heading glow — pass a counter that advances between frames of a live
    output, or leave it at 0 for a static render.
    """
    from xli.ui.markdown import render_rows

    if width is None:
        width = terminal_width()
    if enabled is None:
        enabled = ansi_enabled()

    rows = render_rows(markdown, width)
    if not enabled:
        return "\n".join("".join(text for text, _ in row).rstrip() for row in rows)

    out: list[str] = []
    for row in rows:
        parts: list[str] = []
        for text, style in row:
            if style == RULE_STYLE and enabled:
                # A rule is one continuous object: draw it with the animated
                # gradient, not per-span, so the travelling spot is visible.
                parts.append(gradient_rule(len(text), char="─", tick=tick, depth=color_depth()))
                continue
            parts.append(paint_span(text, style, enabled=enabled, tick=tick))
        out.append("".join(parts).rstrip())
    return "\n".join(out)


def paint_status(text: str, kind: str = "good", *, enabled: bool | None = None) -> str:
    """Colour one status word consistently across front ends."""
    if enabled is None:
        enabled = ansi_enabled()
    glow = {"good": GOOD_GLOW, "warn": WARN_GLOW, "bad": BAD_GLOW}.get(kind)
    if glow is None or not enabled:
        return text
    depth = color_depth()
    return f"{fg(glow.stops[0], depth=depth)}{text}\033[0m"


__all__ = [
    "ANSI_FOR_STYLE",
    "GLOWING",
    "ansi_enabled",
    "paint_span",
    "paint_status",
    "render_markdown_ansi",
    "terminal_width",
]
