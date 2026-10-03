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
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}"


__all__ = [
    "BLOCKS",
    "bars",
    "braille_plot",
    "histogram",
    "sparkline",
    "timeline",
    "tree",
]
