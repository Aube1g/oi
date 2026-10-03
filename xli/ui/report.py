#!/usr/bin/env python3
"""XLI run report — what happened, as a picture.

An agent run ends with a sentence (``[done] fixed the failing tests``) and no
sense of shape: how many steps, which tools, what was slow, whether a delegate
did the work. This module turns the event stream the front ends already
receive into a small set of numbers and one panel that shows them:

    ╭──── run ─────────────────────────────╮
    │ ◆ xli → reviewer         3 delegate  │
    │ steps 7 · tools 19 · errors 1 · 42.3s │
    │ ▁▂▃▅▇▅▃▂ step shapes                 │
    │ read     ████████████ 8               │
    │ bash     ██████ 4                     │
    ╰──────────────────────────────────────╯

Everything is derived from events, so the CLI, the REPL and the TUI all get
the same summary without sharing any state beyond the list of events.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from xli.ui import charts
from xli.ui.agents import agent_color
from xli.ui.gradient import (
    ACCENT_GLOW,
    XLI_AMBER,
    XLI_MINT,
    XLI_RED,
    box,
    color_depth,
    fg,
    paint,
)
from xli.ui.summary import summarise_call

#: Above this many tools the histogram keeps only the busiest ones.
TOP_TOOLS = 8


@dataclass
class RunStats:
    """Numbers extracted from an event stream."""

    steps: int = 0
    max_steps: int = 0
    tools: int = 0
    errors: int = 0
    seconds: float = 0.0
    tool_counts: Counter = field(default_factory=Counter)
    tool_ms: Counter = field(default_factory=Counter)
    step_seconds: list[float] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    agent_steps: Counter = field(default_factory=Counter)
    stopped_reason: str = ""
    summary: str = ""
    ok: bool = True

    def __post_init__(self) -> None:
        # Callers may hand in plain dicts (the demo, a session replay); the
        # counters are what the report needs, so normalise once here instead
        # of defending at every use site.
        self.tool_counts = Counter(self.tool_counts)
        self.tool_ms = Counter(self.tool_ms)

    # ------------------------------------------------------------------ shape
    def tool_histogram(self, limit: int = TOP_TOOLS) -> list[tuple[str, float]]:
        return self.tool_counts.most_common(limit)

    def slowest(self, limit: int = 3) -> list[tuple[str, float]]:
        if not self.tool_ms:
            return []
        return sorted(self.tool_ms.items(), key=lambda pair: -pair[1])[:limit]

    def any_data(self) -> bool:
        return bool(self.steps or self.tools or self.agent_steps)


def stats_from_events(events: list[dict[str, Any]], *, seconds: float = 0.0) -> RunStats:
    """Fold an event list into a :class:`RunStats`.

    Accepts both the CLI's ``{"event": kind, ...}`` records and bare
    ``(kind, payload)`` pairs, because the three front ends store events
    differently and none of them should be forced to change shape.
    """
    stats = RunStats(seconds=seconds)
    pending: dict[str, float] = {}
    for index, record in enumerate(events):
        if isinstance(record, tuple) and len(record) == 2:
            kind, payload = record[0], record[1] or {}
        else:
            payload = dict(record)
            kind = payload.pop("event", "")
        name = str(payload.get("agent_name") or "")
        is_sub = payload.get("agent_id", "main") != "main"
        if name and is_sub and name not in stats.agents:
            stats.agents.append(name)

        if kind == "step":
            stats.steps = max(stats.steps, int(payload.get("index") or 0))
            stats.max_steps = int(payload.get("max_steps") or stats.max_steps)
            if name and is_sub:
                stats.agent_steps[name] += 1
        elif kind == "tool_call":
            stats.tools += 1
            tool = str(payload.get("name", "?"))
            stats.tool_counts[tool] += 1
            pending[f"{index}:{tool}"] = 0.0
        elif kind == "tool_result":
            if not payload.get("ok"):
                stats.errors += 1
            tool = str(payload.get("name", "?"))
            duration = float(payload.get("duration_ms") or 0.0)
            if duration:
                stats.tool_ms[tool] += duration
        elif kind == "agent":
            phase = payload.get("phase")
            if phase == "end":
                reason = str(payload.get("stopped_reason") or "")
                if not is_sub:
                    stats.stopped_reason = reason
                    stats.ok = reason in ("done", "no_tool_calls")
                    stats.seconds = float(payload.get("seconds") or stats.seconds)
            elif phase == "start" and is_sub and name and name not in stats.agents:
                stats.agents.append(name)
    return stats


# ------------------------------------------------------------------- painting
def render_report_ansi(
    stats: RunStats,
    *,
    width: int = 72,
    tick: int = 0,
    enabled: bool | None = None,
    depth: str | None = None,
) -> str:
    """The run panel as ANSI, ready to print."""
    if enabled is None:
        enabled = color_depth() != "none"
    depth = depth or color_depth()

    lines: list[str] = []
    agents = ", ".join(stats.agents) if stats.agents else "xli"
    delegates = f"  {len(stats.agents)} delegate(s)" if stats.agents else ""
    lines.append(
        paint(f"◆ {agents}", ACCENT_GLOW.stops[1], bold=True, enabled=enabled, depth=depth)
        + paint(delegates, XLI_MINT, enabled=enabled, depth=depth)
    )
    counts = (
        f"steps {stats.steps}/{stats.max_steps or '?'} · tools {stats.tools} · "
        f"errors {stats.errors} · {stats.seconds:.1f}s"
    ) if stats.steps else (
        f"tools {stats.tools} · errors {stats.errors} · {stats.seconds:.1f}s"
    )
    lines.append(paint(counts, None, dim=True, enabled=enabled, depth=depth))

    if stats.tool_counts:
        # No padding: a sparkline stretched with its own last value turns a
        # cliff into a plateau and lies about the shape of the run.
        spark = charts.sparkline([count for _, count in stats.tool_counts.most_common()])
        if spark:
            lines.append(
                paint(spark, XLI_MINT, enabled=enabled, depth=depth)
                + paint("  calls", None, dim=True, enabled=enabled, depth=depth)
            )
    if stats.step_seconds:
        spark = charts.sparkline(stats.step_seconds, width=min(40, len(stats.step_seconds)))
        lines.append(
            paint(spark, None, enabled=enabled, depth=depth)
            + paint("  step time", None, dim=True, enabled=enabled, depth=depth)
        )
    if stats.tool_counts:
        lines.append("")
        for bar_line in charts.bars(stats.tool_histogram(), width=min(28, width - 22)):
            # The bar keeps its violet gradient look by painting the label
            # separately; a per-cell gradient here would cost more than it adds.
            lines.append(paint(bar_line, None, enabled=enabled, depth=depth))
    slow = stats.slowest()
    if slow and slow[0][1] >= 50:
        lines.append("")
        lines.append(paint("slowest", None, dim=True, enabled=enabled, depth=depth))
        for tool, ms in slow:
            colour = XLI_RED if ms > 5000 else XLI_AMBER
            lines.append(
                paint(f"  {tool}", colour, enabled=enabled, depth=depth)
                + paint(f"  {ms / 1000:.1f}s", None, dim=True, enabled=enabled, depth=depth)
            )
    if stats.summary:
        lines.append("")
        lines.append(paint(stats.summary.splitlines()[0][:width], None, enabled=enabled, depth=depth))
    return box(lines, width=width, title="run", tick=tick, enabled=enabled, depth=depth)


def report_rows(stats: RunStats, width: int, *, tick: int = 0) -> list[list[tuple[str, str]]]:
    """The same report as TUI rows (``(text, style)`` spans).

    The TUI cannot use ANSI, so this path builds spans with the style names the
    curses palette already knows, plus ``#RRGGBB`` for the agent colour.
    """
    from xli.tui.palette import ACCENT, BAD, DIM, GOOD, HEADING, NORMAL, WARN

    rows: list[list[tuple[str, str]]] = []
    agents = stats.agents or ["xli"]
    head: list[tuple[str, str]] = []
    for index, name in enumerate(agents):
        head.append(("◆ " if index == 0 else "◆ ", agent_color(name)))
        head.append((name + (" " if index == len(agents) - 1 else ", "), agent_color(name)))
    if len(agents) > 1:
        head.append((f"{len(agents)} delegates", DIM))
    rows.append(head)

    parts = (
        f"steps {stats.steps}/{stats.max_steps or '?'} · tools {stats.tools} · "
        f"errors {stats.errors} · {stats.seconds:.1f}s"
    ) if stats.steps else f"tools {stats.tools} · errors {stats.errors} · {stats.seconds:.1f}s"
    rows.append([(parts, DIM)])

    if stats.tool_counts:
        rows.append([
            ("spark ", DIM),
            (charts.sparkline([count for _, count in stats.tool_counts.most_common()]), GOOD),
        ])
        rows.append([("", NORMAL)])
        for line in charts.bars(stats.tool_histogram(), width=max(10, width - 26)):
            label, _, rest = line.partition("  ")
            rows.append([(label + "  ", HEADING), (rest, ACCENT)])
    slow = stats.slowest()
    if slow and slow[0][1] >= 50:
        rows.append([("slowest", DIM)])
        for tool, ms in slow:
            rows.append(
                [(f"  {tool}  ", BAD if ms > 5000 else WARN), (f"{ms / 1000:.1f}s", DIM)]
            )
    if stats.summary:
        rows.append([("", NORMAL)])
        rows.append([(stats.summary.splitlines()[0][:width], NORMAL)])
    return rows


def format_tool_line(name: str, args: dict[str, Any], *, width: int = 100) -> str:
    """A tool call as one line: ``read  src/main.py c 340``."""
    from xli.tui.palette import tool_glyph

    return f"{tool_glyph(name)} {name} {summarise_call(name, args)}"[:width]


__all__ = [
    "RunStats",
    "format_tool_line",
    "render_report_ansi",
    "report_rows",
    "stats_from_events",
]
