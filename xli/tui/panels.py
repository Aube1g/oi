#!/usr/bin/env python3
"""XLI TUI panels — the side views: plan, graph, agents, statistics.

The transcript answers "what happened". These answer the three questions you
ask *while* the agent works:

* **план** — what is left, and what it is doing right now;
* **граф** — what depends on the file being edited (so you can judge the blast
  radius before a change lands);
* **агенты** — who is working, in whose colour;
* **статистика** — how long it has been going, what it costs, where the time
  went.

Each function takes plain data and returns styled rows — no curses, no I/O in
the render path, so a panel can be tested (and screenshotted) without a
terminal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from xli.tui.frame import body_row, bottom_row, separator_row, title_row, tab_strip
from xli.tui.palette import ACCENT, BAD, DIM, FRAME, GAUGE, GOOD, NORMAL, WARN, Row, span
from xli.ui.text import display_width, truncate as truncate_cells

#: (key, locale-key) — the tab order, also the 1..4 shortcuts.
PANELS: tuple[tuple[str, str], ...] = (
    ("plan", "tui_panel_plan"),
    ("graph", "tui_panel_graph"),
    ("agents", "tui_panel_agents"),
    ("stats", "tui_panel_stats"),
)

PANEL_KEYS = ("1", "2", "3", "4")

#: Plan step markers. Text glyphs, not emoji: they render at one cell in every
#: terminal that can draw box-drawing characters at all.
MARK_DONE = "✓"
MARK_NOW = "◐"
MARK_TODO = "☐"
MARK_FAIL = "✗"


@dataclass
class AgentCard:
    """One delegate as the panel shows it."""

    name: str
    colour: str = ACCENT
    status: str = "работает"
    steps: int = 0
    seconds: float = 0.0
    task: str = ""


@dataclass
class PlanItem:
    text: str
    done: bool = False
    failed: bool = False
    current: bool = False


@dataclass
class PanelState:
    """Everything the panels draw, gathered once per repaint."""

    plan: list[PlanItem] = field(default_factory=list)
    agents: list[AgentCard] = field(default_factory=list)
    graph_rows: list[Row] = field(default_factory=list)
    graph_title: str = ""
    counters: dict[str, Any] = field(default_factory=dict)
    step_seconds: list[float] = field(default_factory=list)
    tool_counts: dict[str, int] = field(default_factory=dict)
    started: float = 0.0
    now: float = 0.0
    activity: str = ""


def tabs(active: int, width: int) -> Row:
    from xli.ui.locale import t

    return tab_strip(width, [(t(key), PANEL_KEYS[index]) for index, (_, key) in enumerate(PANELS)], active=active)


def panel_rows(
    name: str,
    state: PanelState,
    width: int,
    height: int,
    *,
    active: int = 0,
    tick: int = 0,
) -> list[Row]:
    """The rows for one panel, fitted to `height` and framed to `width`."""
    body: list[Row]
    if name == "plan":
        body = _plan_rows(state, width - 4)
    elif name == "graph":
        body = state.graph_rows or [[span("считаю граф зависимостей…", DIM)]]
    elif name == "agents":
        body = _agent_rows(state, width - 4)
    else:
        body = _stats_rows(state, width - 4)

    rows: list[Row] = [tabs(active, width)]
    inner_height = max(0, height - 2)  # tab strip + bottom hint
    for index in range(inner_height):
        row = body[index] if index < len(body) else []
        rows.append(body_row(row, width))
    from xli.ui.locale import t

    rows.append(bottom_row(width, [span(t("tui_panel_hint"), DIM)]))
    return rows


def _plan_rows(state: PanelState, width: int) -> list[Row]:
    from xli.ui.locale import t

    rows: list[Row] = []
    if not state.plan:
        rows.append([span(t("tui_plan_empty"), DIM)])
        return rows

    done = sum(1 for item in state.plan if item.done)
    total = len(state.plan)
    filled = int((done / total) * min(24, max(8, width - 24))) if total else 0
    gauge_cells = min(24, max(8, width - 24))
    rows.append(
        [
            span("прогресс ", DIM),
            span("█" * filled, ACCENT),
            span("░" * (gauge_cells - filled), GAUGE),
            span(f"  {t('tui_plan_progress', done=done, total=total)}", DIM),
        ]
    )
    rows.append([])
    for index, item in enumerate(state.plan[: max(1, len(state.plan))], start=1):
        if item.failed:
            mark, style = MARK_FAIL, BAD
        elif item.done:
            mark, style = MARK_DONE, GOOD
        elif item.current:
            mark, style = MARK_NOW, ACCENT
        else:
            mark, style = MARK_TODO, DIM
        label = truncate_cells(item.text, max(10, width - 8))
        rows.append([span(f" {index:>2} ", DIM), span(f"{mark} ", style), span(label, NORMAL if not item.done else DIM)])
    return rows


def _agent_rows(state: PanelState, width: int) -> list[Row]:
    from xli.ui.locale import t

    if not state.agents:
        return [[span(t("tui_agents_empty"), DIM)]]
    rows: list[Row] = []
    for card in state.agents:
        rows.append(
            [
                span("◆ ", card.colour),
                span(card.name, card.colour),
                span(f"  {card.status}", DIM),
            ]
        )
        detail = f"{card.steps} шагов · {card.seconds:.1f} с"
        if card.task:
            detail = truncate_cells(card.task, max(10, width - 14)) + "  " + detail
        rows.append([span("   ", DIM), span(detail, DIM)])
    return rows


def _stats_rows(state: PanelState, width: int) -> list[Row]:
    rows: list[Row] = []
    counters = state.counters or {}
    elapsed = max(0.0, (state.now or state.started) - state.started)
    rows.append(
        [
            span("шагов ", DIM),
            span(str(counters.get("steps", 0)), NORMAL),
            span("  инструментов ", DIM),
            span(str(counters.get("tools", 0)), NORMAL),
            span("  ошибок ", DIM),
            span(str(counters.get("errors", 0)), BAD if counters.get("errors") else DIM),
        ]
    )
    rows.append([span(f"время {elapsed:.1f} с", DIM)] + ([span(f"   {state.activity}", ACCENT)] if state.activity else []))

    if state.step_seconds:
        from xli.ui.charts import braille_plot, sparkline

        rows.append([])
        rows.append([span("время на шаг", DIM)])
        spark = sparkline(state.step_seconds, width=max(8, width - 4))
        rows.append([span(spark, ACCENT)])
        plot = braille_plot(state.step_seconds, width=max(8, width - 4), height=4)
        for line in plot:
            rows.append([span(line, GAUGE)])

    if state.tool_counts:
        rows.append([])
        rows.append([span("инструменты", DIM)])
        top = max(state.tool_counts.values()) or 1
        label_width = max(6, min(14, width - 20))
        for name, count in sorted(state.tool_counts.items(), key=lambda pair: -pair[1])[:6]:
            bar = int(count / top * max(4, width - label_width - 8))
            rows.append(
                [
                    span(f" {truncate_cells(name, label_width):<{label_width}} ", NORMAL),
                    span("█" * bar, ACCENT),
                    span(f" {count}", DIM),
                ]
            )
    return rows


__all__ = [
    "MARK_DONE",
    "MARK_FAIL",
    "MARK_NOW",
    "MARK_TODO",
    "PANEL_KEYS",
    "PANELS",
    "AgentCard",
    "PanelState",
    "PlanItem",
    "panel_rows",
    "tabs",
]
