#!/usr/bin/env python3
"""XLI charts — text-mode graphs for the CLI, the REPL and the TUI.

The agent produces numbers all the time: steps taken, tools called, tokens
burned, milliseconds per tool, modules compiled, files touched. Printing them
as sentences hides the shape; a sparkline or a bar chart shows it in one look.

Everything here is pure: values in, strings out. Colour is applied by the
caller through :mod:`xli.ui.gradient`, so the same chart renders flat into a
log file and glowing into a terminal. Nothing in this module imports curses or
writes to stdout.

The vocabulary:

``sparkline``      one line, one value per cell  ▁▂▃▅▇
``bars``           labelled horizontal bars with counts
``braille_plot``   a real 2-D line chart on a braille grid (2×4 dots per cell)
``histogram``      bucketed distribution
``tree``           box-drawing hierarchy (agents → steps → tools)
``timeline``       one row per step, proportional durations
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

#: Eight steps of vertical resolution, from empty to full block.
BLOCKS = "▁▂▃▄▅▆▇█"
#: Horizontal fill pair.
BAR_FULL = "█"
BAR_EMPTY = "░"
#: Braille dot bit for (column, row) — row 0 is the top.
_DOT = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))


def sparkline(values: Sequence[float], *, width: int | None = None) -> str:
    """A one-line graph. Values are resampled to ``width`` cells if needed.

    A flat series renders as mid-height blocks rather than an empty line: "no
    change" is information, and a blank strip reads as "no data".
    """
    data = [float(v) for v in values]
    if not data:
        return ""
    if width is None:
        width = len(data)
    if width <= 0:
        return ""
    if len(data) > width:
        data = _resample(data, width)
    elif len(data) < width:
        # Repeat the last value so the graph is padded, not stretched.
        data = data + [data[-1]] * (width - len(data))

    low, high = min(data), max(data)
    if high == low:
        return BLOCKS[3] * len(data)
    span = high - low
    return "".join(BLOCKS[min(7, int((value - low) / span * 7.999))] for value in data)


def _resample(data: list[float], width: int) -> list[float]:
    """Average adjacent samples down to ``width`` buckets."""
    if width >= len(data):
        return data
    out: list[float] = []
    for index in range(width):
        start = int(index * len(data) / width)
        end = max(start + 1, int((index + 1) * len(data) / width))
        bucket = data[start:end]
        out.append(sum(bucket) / len(bucket))
    return out


def bars(
    items: Iterable[tuple[str, float]],
    *,
    width: int = 40,
    label_width: int | None = None,
    show_value: bool = True,
    empty: str = BAR_EMPTY,
) -> list[str]:
    """Labelled horizontal bars, longest first.

    Returns lines of the form ``read         ████████████ 12``. The caller
    colours them; the bar itself is a plain string so width maths is trivial.
    """
    rows = [(str(label), float(value)) for label, value in items]
    if not rows:
        return []
    if label_width is None:
        label_width = max(len(label) for label, _ in rows)
    peak = max(value for _, value in rows) or 1.0
    lines: list[str] = []
    for label, value in rows:
        filled = max(0, min(width, round(value / peak * width)))
        tail = f" {_human(value)}" if show_value else ""
        lines.append(
            f"{label.ljust(label_width)}  {BAR_FULL * filled}{empty * (width - filled)}{tail}"
        )
    return lines


def histogram(values: Sequence[float], *, buckets: int = 5, width: int = 32) -> list[str]:
    """Bucketed counts, with the range printed per row."""
    data = [float(v) for v in values]
    if not data:
        return []
    low, high = min(data), max(data)
    if high == low:
        low, high = low - 0.5, high + 0.5
    step = (high - low) / buckets
    counts = [0] * buckets
    for value in data:
        index = min(buckets - 1, int((value - low) / step))
        counts[index] += 1
    spans = [
        (f"{low + index * step:>6.1f}–{low + (index + 1) * step:<6.1f}", count)
        for index, count in enumerate(counts)
    ]
    return bars(spans, width=width, label_width=15)


def braille_plot(
    series: Sequence[float] | Sequence[Sequence[float]],
    *,
    width: int = 60,
    height: int = 8,
    y_range: tuple[float, float] | None = None,
) -> list[str]:
    """A line plot on a braille grid: 2 pixels per cell across, 4 down.

    ``series`` is either one sequence (one line) or several (overlaid lines).
    Overlaid lines are not colourless — the caller gets the same grid back and
    can assign colours per row; here every dot is just set.
    """
    if height <= 0 or width <= 0:
        return []
    lines: list[list[float]] = []
    if series and isinstance(series[0], (int, float)):
        lines = [[float(v) for v in series]]  # type: ignore[arg-type]
    else:
        lines = [[float(v) for v in line] for line in series]  # type: ignore[arg-type]
    lines = [line for line in lines if line]
    if not lines:
        return []

    values = [value for line in lines for value in line]
    low, high = y_range if y_range else (min(values), max(values))
    if high == low:
        low, high = low - 0.5, high + 0.5

    grid = [[0] * (width * 2) for _ in range(height * 4)]

    def plot(line: list[float]) -> None:
        points: list[tuple[int, int]] = []
        for index, value in enumerate(line):
            x = 0 if len(line) == 1 else round(index / (len(line) - 1) * (width * 2 - 1))
            y = round((1 - (value - low) / (high - low)) * (height * 4 - 1))
            points.append((x, max(0, min(height * 4 - 1, y))))
        previous: tuple[int, int] | None = None
        for x, y in points:
            if previous is not None:
                _line(grid, previous, (x, y))
            else:
                grid[y][x] = 1
            previous = (x, y)

    for line in lines:
        plot(line)

    out: list[str] = []
    for cell_y in range(height):
        row: list[str] = []
        for cell_x in range(width):
            bits = 0
            for dot_y in range(4):
                for dot_x in range(2):
                    if grid[cell_y * 4 + dot_y][cell_x * 2 + dot_x]:
                        bits |= _DOT[dot_x][dot_y]
            row.append(chr(0x2800 + bits) if bits else " ")
        out.append("".join(row))
    return out


def _line(grid: list[list[int]], start: tuple[int, int], end: tuple[int, int]) -> None:
    """Bresenham, so a steep jump between samples is a line, not a gap."""
    x0, y0 = start
    x1, y1 = end
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    error = dx + dy
    while True:
        grid[y0][x0] = 1
        if x0 == x1 and y0 == y1:
            return
        doubled = 2 * error
        if doubled >= dy:
            error += dy
            x0 += sx
        if doubled <= dx:
            error += dx
            y0 += sy


def tree(entries: Iterable[tuple[str, int]], *, title: str = "") -> list[str]:
    """A hierarchy from ``(name, depth)`` pairs — agents, steps, tools.

    Depth is explicit rather than inferred from a parent pointer: the callers
    are flat event streams, and asking them for a parent id would be a worse
    interface than asking them for indentation.
    """
    rows = list(entries)
    if not rows:
        return []
    out: list[str] = [title] if title else []
    max_depth = max(depth for _, depth in rows)
    counters = [0] * (max_depth + 2)
    for name, depth in rows:
        counters[depth] += 1
        out.append(f"{'│  ' * depth}{'├─' if depth else '◆'} {name}")
    return out


def timeline(
    steps: Iterable[tuple[str, float]],
    *,
    width: int = 40,
    label_width: int | None = None,
) -> list[str]:
    """One row per step with a proportional duration bar (Gantt-lite)."""
    rows = [(str(label), max(0.0, float(seconds))) for label, seconds in steps]
    if not rows:
        return []
    if label_width is None:
        label_width = max(len(label) for label, _ in rows)
    total = sum(seconds for _, seconds in rows) or 1.0
    out: list[str] = []
    for label, seconds in rows:
        cells = max(0, min(width, round(seconds / total * width)))
        out.append(f"{label.ljust(label_width)}  {'▏' + '█' * max(0, cells - 1)}{'' if cells else '·'}")
    return out


def _human(value: float) -> str:
    """A number the way the interface says it: `2,3 млн`, never `2.3M`.

    Localised through `xli.ui.locale`, because the charts are part of the
    interface and the project's rule is that they read like Russian.
    """
    try:
        from xli.ui.locale import number_word

        return number_word(value)
    except Exception:  # noqa: BLE001 - a chart must render without the locale
        if value == int(value):
            return str(int(value))
        return f"{value:.2f}"


def heatmap(
    matrix: Sequence[Sequence[float]],
    *,
    row_labels: Sequence[str] = (),
    col_labels: Sequence[str] = (),
    cells: str = " ░▒▓█",
) -> list[str]:
    """A grid of intensities — good for activity over time or per module.

    Cells are chosen from a ramp rather than coloured, so the shape survives a
    plain-text log; the caller may still tint the whole block.
    """
    rows = [[float(value) for value in row] for row in matrix]
    if not rows:
        return []
    flat = [value for row in rows for value in row] or [0.0]
    low, high = min(flat), max(flat)
    span = (high - low) or 1.0
    label_width = max((len(str(label)) for label in row_labels), default=0)
    out: list[str] = []
    if col_labels:
        pad = " " * label_width
        out.append(pad + "  " + " ".join(str(label)[:1] for label in col_labels))
    for index, row in enumerate(rows):
        label = str(row_labels[index]) if index < len(row_labels) else ""
        line = "".join(
            cells[min(len(cells) - 1, max(0, int((value - low) / span * (len(cells) - 1) + 0.5)))]
            for value in row
        )
        out.append(f"{label.ljust(label_width)}  {line}" if label_width else line)
    return out


def stacked(
    items: Iterable[tuple[str, Sequence[float]]],
    *,
    width: int = 40,
    label_width: int | None = None,
    marks: str = "█▓▒░",
) -> list[str]:
    """One bar per row, split into segments — a breakdown, not a total."""
    rows = [(str(label), [max(0.0, float(value)) for value in values]) for label, values in items]
    if not rows:
        return []
    if label_width is None:
        label_width = max(len(label) for label, _ in rows)
    peaks = [sum(values) for _, values in rows]
    peak = max(peaks) or 1.0
    out: list[str] = []
    for label, values in rows:
        total = sum(values)
        cells = max(1, round(total / peak * width)) if total else 0
        allocation = [
            int(round(value / total * cells)) if total else 0 for value in values
        ]
        # Give the rounding remainder to the largest segment, so the bar is
        # always exactly `cells` wide — a ragged bar looks like a bug.
        if allocation and cells:
            allocation[allocation.index(max(allocation))] += cells - sum(allocation)
        bar = ""
        for index, count in enumerate(allocation):
            bar += marks[index % len(marks)] * max(0, count)
        out.append(f"{label.ljust(label_width)}  {bar} {_human(total)}")
    return out


def treemap(
    entries: Iterable[tuple[str, float]],
    *,
    width: int = 60,
    height: int = 12,
) -> list[str]:
    """Squarified treemap: area proportional to value, labels where they fit.

    Sizes are the one thing a bar chart cannot show, because a bar chart is
    about magnitude and a treemap is about magnitude *and* share.
    """
    rows = sorted(
        ((str(name), max(0.0, float(value))) for name, value in entries),
        key=lambda item: -item[1],
    )
    rows = [(name, value) for name, value in rows if value > 0]
    if not rows or width < 8 or height < 3:
        return []

    grid = [[" "] * width for _ in range(height)]
    labels: list[tuple[int, int, str, str]] = []
    #: Distinct fills rather than one: without colour, a treemap drawn in a
    #: single shade is an undifferentiated rectangle with words in it.
    fills = "▓▒░█▚▞◧◨"
    block = [0]

    def layout(items: list[tuple[str, float]], x: int, y: int, w: int, h: int) -> None:
        if not items or w <= 1 or h <= 1:
            return
        if len(items) == 1:
            name, value = items[0]
            fill = fills[block[0] % len(fills)]
            block[0] += 1
            for row in range(y, y + h):
                for col in range(x, x + w):
                    grid[row][col] = fill
            labels.append((x + 1, y + h // 2, name[: max(1, w - 2)], fill))
            return

        half = 0.0
        split = 0
        subtotal = sum(value for _, value in items)
        for index, (_, value) in enumerate(items):
            half += value
            split = index + 1
            if half >= subtotal / 2:
                break
        first, rest = items[:split], items[split:]
        fraction = sum(value for _, value in first) / subtotal

        if w >= h:
            cut = max(2, min(w - 2, int(round(w * fraction))))
            layout(first, x, y, cut, h)
            layout(rest, x + cut, y, w - cut, h)
        else:
            cut = max(2, min(h - 2, int(round(h * fraction))))
            layout(first, x, y, w, cut)
            layout(rest, x, y + cut, w, h - cut)

    layout(rows, 0, 0, width, height)

    for x, y, text, fill in labels:
        for index, char in enumerate(text):
            if 0 <= y < height and 0 <= x + index < width and grid[y][x + index] == fill:
                grid[y][x + index] = char
    out = ["".join(row) for row in grid]
    out.append("  ".join(f"{name}: {_human(value)}" for name, value in rows[:6]))
    return out


def gauge(fraction: float, *, width: int = 30, label: str = "") -> str:
    """A single-ratio bar: quota used, budget spent, progress made."""
    ratio = max(0.0, min(1.0, float(fraction)))
    filled = int(round(ratio * width))
    bar = "█" * filled + "░" * (width - filled)
    percent = f"{ratio * 100:.0f}%"
    return f"{label + ' ' if label else ''}{bar} {percent}"


def table(
    rows: Sequence[Sequence[object]],
    *,
    headers: Sequence[str] = (),
    align: str = "left",
    separator: str = "  ",
) -> list[str]:
    """An aligned table — the least glamorous and most-read chart there is."""
    body = [[str(cell) for cell in row] for row in rows]
    if not body and not headers:
        return []
    columns = max([len(row) for row in body] + ([len(headers)] if headers else [0]))
    widths = [0] * columns
    for row in ([list(headers)] if headers else []) + body:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def render(row: Sequence[str]) -> str:
        cells = []
        for index in range(columns):
            cell = row[index] if index < len(row) else ""
            if align == "right" and index:
                cells.append(cell.rjust(widths[index]))
            elif align == "center" and index:
                cells.append(cell.center(widths[index]))
            else:
                cells.append(cell.ljust(widths[index]))
        return separator.join(cells).rstrip()

    out = []
    if headers:
        out.append(render(list(headers)))
        out.append(separator.join("─" * width for width in widths))
    out.extend(render(row) for row in body)
    return out


def _spark_series(series: Sequence[Sequence[float]]) -> str:
    """Unused placeholder kept out of __all__: multi-series sparklines are a
    legibility problem, not a feature."""
    return ""


def normalise_series(data: Sequence[float], *, height: int = 8) -> list[list[float]]:
    """Scale values into `height` rows for the braille grid."""
    if not data:
        return []
    low, high = min(data), max(data)
    span = (high - low) or 1.0
    return [[(value - low) / span * (height - 1)] for value in data]


def _round_sig(value: float) -> float:
    return float(f"{value:.3g}") if math.isfinite(value) else 0.0


__all__ = [
    "BLOCKS",
    "bars",
    "braille_plot",
    "gauge",
    "heatmap",
    "histogram",
    "sparkline",
    "stacked",
    "table",
    "timeline",
    "treemap",
    "tree",
]
