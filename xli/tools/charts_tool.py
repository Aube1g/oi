#!/usr/bin/env python3
"""The `chart` tool — the agent drawing its own graphs, in the terminal.

Numbers the agent collects (file sizes, step durations, test counts, token
spend, module degree) are most useful as a picture, and the picture has to be
text: the user is looking at a terminal, not a browser, and an image file
cannot be scrolled, copied into a report, or read over SSH.

The tool is a rendering service, not a data collector. The agent decides *what*
to measure — it has `bash`, `grep`, `dep_graph` and the MCP codebase server for
that — and asks for a shape here. Six shapes are supported so the same data can
be shown the way that reads best:

``bar``       labelled magnitudes, longest first
``line``      a real 2-D plot on a braille grid
``spark``     one line, for a series inside a sentence
``hist``      distribution, when the shape of the spread is the point
``timeline``  proportional durations, one row per item
``stacked``   a breakdown inside each total
``tree``      a hierarchy with box-drawing
``heat``      a grid of intensities (activity per day/module)
``treemap``   area proportional to share, labelled
``table``     aligned columns, for mixed types
``gauge``     one ratio: budget, quota, progress

Every renderer returns plain text, so the agent can quote it back in its answer
and the user sees the graph even when tool output is collapsed.
"""

from __future__ import annotations

import json
from typing import Any

from xli.tools.base import Param, ToolError, ToolResult, tool
from xli.ui import charts

#: The kinds this tool knows, and what each expects. The description is shown
#: to the model, so it says what the shape is *for*, not just its name.
KINDS: dict[str, str] = {
    "bar": "items: [[label, value], ...] — magnitudes, longest first",
    "line": "series: [v0, v1, ...] or items: [[label, value], ...] — a plot",
    "spark": "series: [v0, v1, ...] — one line",
    "hist": "series: [v0, v1, ...] — distribution of the values",
    "timeline": "items: [[label, seconds], ...] — durations, proportional",
    "stacked": "items: [[label, [part, part, ...]], ...] — a breakdown per row",
    "tree": "items: [[name, depth], ...] — a hierarchy by indentation",
    "heat": "matrix: [[...], ...] with optional row_labels / col_labels",
    "treemap": "items: [[label, value], ...] — share of a whole, as area",
    "table": "rows: [[cell, ...], ...] with optional headers",
    "gauge": "value: 0..1 — one ratio, with an optional label",
}


def _as_pairs(items: Any, *, what: str) -> list[tuple[str, float]]:
    if not isinstance(items, list):
        raise ToolError(f"{what}: expected a list of [label, value] pairs")
    out: list[tuple[str, float]] = []
    for entry in items:
        if isinstance(entry, dict):
            label = entry.get("label") or entry.get("name") or ""
            value = entry.get("value") or entry.get("count") or entry.get("size") or 0
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            label, value = entry[0], entry[1]
        else:
            raise ToolError(f"{what}: each entry must be [label, value], got {entry!r}")
        try:
            out.append((str(label), float(value)))
        except (TypeError, ValueError) as exc:
            raise ToolError(f"{what}: value for {label!r} is not a number") from exc
    if not out:
        raise ToolError(f"{what}: no data")
    return out


def _as_series(series: Any, *, what: str) -> list[float]:
    if isinstance(series, list) and series and isinstance(series[0], (list, tuple, dict)):
        return [value for _label, value in _as_pairs(series, what=what)]
    if not isinstance(series, list):
        raise ToolError(f"{what}: expected a list of numbers")
    try:
        return [float(value) for value in series]
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{what}: every value must be a number") from exc


def render(
    kind: str,
    *,
    series: Any = None,
    items: Any = None,
    rows: Any = None,
    matrix: Any = None,
    headers: Any = None,
    row_labels: Any = None,
    col_labels: Any = None,
    value: float | None = None,
    title: str = "",
    width: int = 0,
    height: int = 0,
) -> list[str]:
    """Render one chart. Shared by the tool and by tests; pure text in/out."""
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        raise ToolError(
            f"unknown chart kind {kind!r}; supported: {', '.join(sorted(KINDS))}"
        )

    width = int(width) if width else 0
    height = int(height) if height else 0
    out: list[str] = []
    if title:
        out.append(title)

    if kind == "bar":
        pairs = _as_pairs(items if items is not None else series, what="bar")
        pairs.sort(key=lambda item: -item[1])
        out.extend(charts.bars(pairs, width=width or 34))
    elif kind == "line":
        data = _as_series(series if series is not None else items, what="line")
        out.extend(charts.braille_plot(data, width=width or 60, height=height or 8))
    elif kind == "spark":
        data = _as_series(series if series is not None else items, what="spark")
        out.append(charts.sparkline(data, width=width or None))
    elif kind == "hist":
        data = _as_series(series if series is not None else items, what="hist")
        out.extend(charts.histogram(data, width=width or 32))
    elif kind == "timeline":
        pairs = _as_pairs(items if items is not None else series, what="timeline")
        out.extend(charts.timeline(pairs, width=width or 40))
    elif kind == "stacked":
        raw = items if items is not None else series
        if not isinstance(raw, list):
            raise ToolError("stacked: expected a list of [label, [part, ...]] rows")
        rows_in: list[tuple[str, list[float]]] = []
        for entry in raw:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                raise ToolError("stacked: each row must be [label, [part, part, ...]]")
            label, parts = entry[0], entry[1]
            if not isinstance(parts, (list, tuple)):
                parts = [parts]
            rows_in.append((str(label), [float(part) for part in parts]))
        out.extend(charts.stacked(rows_in, width=width or 34))
    elif kind == "tree":
        pairs = _as_pairs(items if items is not None else series, what="tree")
        out.extend(charts.tree([(label, int(depth)) for label, depth in pairs]))
    elif kind == "heat":
        if not isinstance(matrix, list) or not matrix:
            raise ToolError("heat: needs a matrix of numbers")
        out.extend(
            charts.heatmap(
                matrix,
                row_labels=[str(label) for label in (row_labels or [])],
                col_labels=[str(label) for label in (col_labels or [])],
            )
        )
    elif kind == "treemap":
        pairs = _as_pairs(items if items is not None else series, what="treemap")
        out.extend(charts.treemap(pairs, width=width or 60, height=height or 12))
    elif kind == "table":
        if not isinstance(rows, list) or not rows:
            raise ToolError("table: needs rows (a list of lists)")
        out.extend(charts.table(rows, headers=[str(h) for h in (headers or [])]))
    elif kind == "gauge":
        if value is None:
            raise ToolError("gauge: needs value between 0 and 1")
        out.append(charts.gauge(float(value), width=width or 30, label=title))
        if title:
            out = out[1:]  # the gauge line already carries the label

    return [line for line in out if line != ""]


@tool(
    "chart",
    "Draw a text chart from data you collected — the result appears in the "
    "terminal and can be quoted in your answer. Kinds: "
    + "; ".join(f"{name} — {hint}" for name, hint in KINDS.items())
    + ". Pass the data as a JSON string in the matching argument "
    "(items/series/rows/matrix). Prefer this over describing numbers in prose: "
    "a bar chart or a treemap shows the shape at a glance.",
    [
        Param("kind", "string", "chart kind, one of: " + ", ".join(sorted(KINDS))),
        Param("items", "string", "JSON [[label, value], ...] for bar/timeline/tree/treemap", default=""),
        Param("series", "string", "JSON [v0, v1, ...] for line/spark/hist", default=""),
        Param("rows", "string", "JSON [[cell, ...], ...] for table", default=""),
        Param("matrix", "string", "JSON [[...], ...] for heat", default=""),
        Param("headers", "string", "JSON [name, ...] for table", default=""),
        Param("row_labels", "string", "JSON labels for heat rows", default=""),
        Param("col_labels", "string", "JSON labels for heat columns", default=""),
        Param("value", "number", "0..1 for gauge", default=-1),
        Param("title", "string", "optional heading printed above the chart", default=""),
        Param("width", "integer", "chart width in cells", default=0),
        Param("height", "integer", "chart height in rows (line, treemap)", default=0),
    ],
    tags=["analysis", "ui"],
)
async def chart(
    kind: str,
    items: str = "",
    series: str = "",
    rows: str = "",
    matrix: str = "",
    headers: str = "",
    row_labels: str = "",
    col_labels: str = "",
    value: float = -1.0,
    title: str = "",
    width: int = 0,
    height: int = 0,
) -> ToolResult:
    """Draw a text chart. See the description for the data each kind wants."""

    def parsed(raw: str, name: str) -> Any:
        text = (raw or "").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ToolError(f"{name} is not valid JSON: {exc}") from exc

    lines = render(
        kind,
        series=parsed(series, "series"),
        items=parsed(items, "items"),
        rows=parsed(rows, "rows"),
        matrix=parsed(matrix, "matrix"),
        headers=parsed(headers, "headers"),
        row_labels=parsed(row_labels, "row_labels"),
        col_labels=parsed(col_labels, "col_labels"),
        value=None if value < 0 else value,
        title=title,
        width=width,
        height=height,
    )
    if not lines:
        raise ToolError("nothing to draw: no data for that kind")
    # The drawing travels as the summary: the CLI, the REPL and the TUI all
    # print a tool result's summary, so a chart in `data` alone would only be
    # visible to whoever looked at the JSON.
    return ToolResult.success(
        data={"kind": kind, "text": "\n".join(lines)},
        summary="\n".join(lines),
    )


#: The decorated function *is* the tool object; the alias is what the registry
#: suite imports, so the name reads as a thing rather than as a verb.
CHART_TOOL = chart

__all__ = ["CHART_TOOL", "KINDS", "chart", "render"]
