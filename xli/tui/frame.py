#!/usr/bin/env python3
"""XLI TUI frame — the box the interface is drawn inside.

A terminal interface reads as *designed* when it has an edge. The frame here is
deliberately thin: a single violet line around the body, a title band on top, a
status band at the bottom, and a tab strip when a panel is open. Everything the
widgets produce is fitted into it, so a long line cannot push the right border
off screen (which is what used to happen: rows were padded with ``len()`` and a
wide glyph or an escape sequence moved the edge).

Every function here builds a row by *filling to the width*, never by
subtracting a fixed amount of chrome — that arithmetic is where off-by-one
borders come from, and a one-cell misalignment is visible on every single line.

The primitives are pure functions of ``(rows, width)`` — no curses — so the
layout stays testable without a terminal.
"""

from __future__ import annotations

from xli.tui.palette import ACCENT, DIM, FRAME, NORMAL, Span, Row, span
from xli.ui.text import display_width, truncate as truncate_cells


def inner_width(width: int) -> int:
    """Content columns available between the two border characters."""
    return max(8, width - 4)


def row_width(row: Row) -> int:
    return sum(display_width(text) for text, _ in row)


def fit(row: Row, width: int) -> Row:
    """Truncate a span row to `width` cells and pad it to exactly `width`."""
    result: Row = []
    used = 0
    for text, style in row:
        remaining = width - used
        if remaining <= 0:
            break
        if display_width(text) > remaining:
            result.append((truncate_cells(text, remaining), style))
            used = width
            break
        result.append((text, style))
        used += display_width(text)
    if used < width:
        result.append((" " * (width - used), NORMAL))
    return result


def _close(row: Row, width: int, corner: str, fill: str = "─", style: str = FRAME) -> Row:
    """Pad `row` with `fill` up to `width - 1` cells and cap it with a corner.

    Filling rather than subtracting keeps the right edge straight whatever the
    content is: one character of `fill` plus one corner is exactly one cell,
    which is the invariant every border depends on.
    """
    out = fit(row, max(0, width - 1))
    used = row_width(out)
    if used < width - 1:
        out.append(((fill) * (width - 1 - used), style))
    out.append((corner, style))
    return out


def body_row(row: Row, width: int, *, right: str = "│", style: str = FRAME) -> Row:
    """Wrap one content row in the left and right borders."""
    return [span("│ ", style)] + fit(row, inner_width(width)) + [span(" " + right, style)]


def title_row(width: int, left: Row, right: Row = None, *, style: str = FRAME) -> Row:
    """``╭─ left ───────── right ─╮`` — the top edge with content inside it."""
    row: Row = [span("╭─ ", style)] + list(left)
    if right:
        row += [span(" ", style)] + list(right)
    return _close(row, width, "╮", style=style)


def separator_row(width: int, *, left: Row = None, right: Row = None, style: str = FRAME) -> Row:
    """``├─ left ───────── right ─┤`` — a divider that can carry labels."""
    row: Row = [span("├─", style)]
    if left:
        row += [span(" ", style)] + list(left)
    if right:
        row += [span(" ", style)] + list(right)
    return _close(row, width, "┤", style=style)


def bottom_row(width: int, content: Row, *, style: str = FRAME) -> Row:
    """``╰─ content ─────╯`` — the bottom edge, carrying the status bar."""
    row: Row = [span("╰─ ", style)] + list(content)
    return _close(row, width, "╯", style=style)


def tab_strip(
    width: int,
    tabs: list[tuple[str, str]],
    *,
    active: int = 0,
    style: str = FRAME,
) -> Row:
    """``├─ [план] ── граф ── агенты ──┤`` — which pane is open, and the others."""
    pieces: Row = []
    for index, (label, key) in enumerate(tabs):
        if index == active:
            pieces += [span(f"{label}", ACCENT), span(" ── ", style)]
        else:
            pieces += [span(f"{label}", DIM), span(f" {key} ── ", style)]
    return separator_row(width, left=pieces[:-1], style=style)


def hint_row(width: int, hint: str, *, style: str = DIM) -> Row:
    return body_row([span(hint, style)], width)


__all__ = [
    "Row",
    "Span",
    "body_row",
    "bottom_row",
    "fit",
    "hint_row",
    "inner_width",
    "row_width",
    "separator_row",
    "tab_strip",
    "title_row",
]
