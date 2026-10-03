#!/usr/bin/env python3
"""XLI render demo — what the front ends look like, on demand.

This exists because the old way to check the rendering was two test files that
ended in ``assert False``: they "passed" by failing and printing the output in
the traceback. That meant the suite was permanently red and nobody could tell
a real regression from the demo.

Now the demo is a command (``xli demo``), the tests assert real properties of
the renderers, and this module is the single sample document they share — so
what you see in the terminal is exactly what the tests check.
"""

from __future__ import annotations

from xli.ui import charts
from xli.ui.agents import agent_color
from xli.ui.gradient import (
    ACCENT_GLOW,
    H1_GLOW,
    color_depth,
    gradient_rule,
    gradient_text,
    paint,
)
from xli.ui.markdown import render_rows
from xli.ui.report import RunStats, render_report_ansi
from xli.ui.syntax import highlight_line

#: Markdown sample that exercises every block kind the parser understands.
SAMPLE_MARKDOWN = """\
# XLI render engine

Терминал умеет больше, чем печатать серый текст. **Жирный**, *курсив*,
`код`, ~~зачёркнутый~~ и [ссылка](https://github.com/Aube1g/oi).

## Списки и задачи

- [x] градиенты и свечение заголовков
- [x] графы: спарклайны, бары, braille-графики
- [ ] ваши идеи

1. шаг первый
2. шаг второй

> Цитата — это вертикальная черта акцентом, а не серый текст.
> Вторая строка остаётся внутри того же блока.

```python
def sparkline(values: list[float], width: int = 40) -> str:
    \"\"\"Одна строка, один график.\"\"\"
    return "".join(BLOCKS[int(v)] for v in values)  # noqa: E501
```

| Возможность | Где видно | Статус |
| --- | --- | --- |
| Градиент | CLI, TUI | готово |
| Графы | CLI, TUI | готово |

---

Последний абзац: рендер должен оставаться читаемым, когда цвета выключены.
"""

#: A tiny dependency graph, for the `graph` section.
SAMPLE_EDGES = (
    ("xli.cli", "xli.agent"),
    ("xli.cli", "xli.tui.app"),
    ("xli.agent", "xli.tools.registry"),
    ("xli.agent", "xli.parse"),
    ("xli.tui.app", "xli.tui.widgets"),
    ("xli.tui.widgets", "xli.ui.markdown"),
    ("xli.ui.markdown", "xli.ui.syntax"),
    ("xli.ui.ansi", "xli.ui.gradient"),
)


def sections() -> list[str]:
    return ["banner", "markdown", "charts", "agents", "report", "graph", "syntax"]


def render_banner(width: int = 78, *, tick: int = 0, enabled: bool | None = None) -> str:
    """The identity block every front end opens with."""
    if enabled is None:
        enabled = color_depth() != "none"
    title = gradient_text("X L I", *H1_GLOW.stops[:2], bold=True, enabled=enabled, tick=tick)
    sub = paint("автономный кодовый агент", None, dim=True, enabled=enabled)
    lines = [
        f"{title}  {sub}",
        "",
        gradient_rule(width, tick=tick, enabled=enabled),
    ]
    return "\n".join(lines)


def render_markdown_section(width: int = 78, *, tick: int = 0, enabled: bool | None = None) -> str:
    """The sample markdown through the shared renderer, with the glow."""
    from xli.ui.ansi import render_markdown_ansi

    return render_markdown_ansi(SAMPLE_MARKDOWN, width, enabled=enabled, tick=tick)


def render_charts_section(*, enabled: bool | None = None) -> str:
    """Every chart in the vocabulary, with the numbers that produced it."""
    if enabled is None:
        enabled = color_depth() != "none"
    steps = [1, 2, 2, 4, 3, 6, 9, 7, 5, 8, 11, 9, 6, 4, 3]
    lines: list[str] = []

    lines.append(paint("sparkline  7 шагов, длительность (сек)", None, dim=True, enabled=enabled))
    lines.append(
        paint(charts.sparkline(steps, width=60), ACCENT_GLOW.stops[2], enabled=enabled)
    )

    lines.append("")
    lines.append(paint("bars       вызовы инструментов", None, dim=True, enabled=enabled))
    lines.extend(
        paint(line, ACCENT_GLOW.stops[1], enabled=enabled)
        for line in charts.bars(
            [("read", 12), ("bash", 7), ("edit", 5), ("grep", 4), ("write", 1)],
            width=32,
        )
    )

    lines.append("")
    lines.append(paint("plot       шаг агента: время на шаг", None, dim=True, enabled=enabled))
    for line in charts.braille_plot(steps, width=60, height=6):
        lines.append(paint(line, ACCENT_GLOW.stops[0], enabled=enabled))

    lines.append("")
    lines.append(paint("histogram  распределение вызовов (мс)", None, dim=True, enabled=enabled))
    lines.extend(
        paint(line, ACCENT_GLOW.stops[1], enabled=enabled)
        for line in charts.histogram([120, 180, 200, 250, 260, 300, 310, 900, 1200], buckets=4)
    )

    lines.append("")
    lines.append(paint("timeline   вклад инструментов", None, dim=True, enabled=enabled))
    lines.extend(
        paint(line, ACCENT_GLOW.stops[2], enabled=enabled)
        for line in charts.timeline([("read", 2.1), ("bash", 8.4), ("edit", 1.2)], width=40)
    )
    return "\n".join(lines)


def render_agents_section(*, enabled: bool | None = None) -> str:
    """The same delegate in the same colour every time."""
    if enabled is None:
        enabled = color_depth() != "none"
    names = ["xli", "reviewer", "test-writer", "debugger", "explorer"]
    lines: list[str] = []
    for name in names:
        colour = agent_color(name, name.replace("-", "") if name != "xli" else "accent")
        badge = ("◆ " if name != "xli" else "◈ ") + name
        lines.append(
            paint(badge.ljust(16), colour, bold=True, enabled=enabled)
            + paint("─ работает над ...", None, dim=True, enabled=enabled)
        )
    return "\n".join(lines)


def render_report_section(*, tick: int = 0, enabled: bool | None = None) -> str:
    """The end-of-run panel, filled with a plausible run."""
    stats = RunStats(
        steps=9,
        max_steps=24,
        tools=29,
        errors=1,
        seconds=42.3,
        tool_counts={"read": 12, "bash": 7, "edit": 5, "grep": 3, "web_search": 2},
        tool_ms={"bash": 8400.0, "web_search": 2100.0, "read": 640.0},
        step_seconds=[1.2, 2.4, 3.1, 1.8, 6.2, 2.0, 4.4, 1.1, 2.9],
        agents=["reviewer", "test-writer"],
        stopped_reason="done",
        summary="Обновил рендер: градиенты, графы и свечение заголовков.",
    )
    return render_report_ansi(stats, width=74, tick=tick, enabled=enabled)


def render_graph_section(*, enabled: bool | None = None) -> str:
    """A dependency graph drawn with box characters, coloured by depth."""
    if enabled is None:
        enabled = color_depth() != "none"
    children: dict[str, list[str]] = {}
    for parent, child in SAMPLE_EDGES:
        children.setdefault(parent, []).append(child)

    lines: list[str] = [paint("xli.cli", ACCENT_GLOW.stops[2], bold=True, enabled=enabled)]

    def walk(node: str, prefix: str, depth: int, seen: set[str]) -> None:
        if depth > 3:
            return
        kids = children.get(node, [])
        for index, kid in enumerate(kids):
            last = index == len(kids) - 1
            colour = ACCENT_GLOW.stops[min(depth, len(ACCENT_GLOW.stops) - 1)]
            marker = "└─ " if last else "├─ "
            cycle = paint("  (цикл)", None, dim=True, enabled=enabled) if kid in seen else ""
            lines.append(paint(prefix + marker, None, dim=True, enabled=enabled)
                         + paint(kid, colour, enabled=enabled) + cycle)
            if kid not in seen:
                walk(kid, prefix + ("   " if last else "│  "), depth + 1, seen | {kid})

    walk("xli.cli", "", 0, {"xli.cli"})
    lines.append("")
    lines.append(paint("braille-график пакетов по размеру", None, dim=True, enabled=enabled))
    sizes = [12, 30, 8, 44, 21, 17, 39, 26, 14, 33, 28, 19]
    for line in charts.braille_plot(sizes, width=60, height=5):
        lines.append(paint(line, ACCENT_GLOW.stops[0], enabled=enabled))
    return "\n".join(lines)


def render_syntax_section(*, enabled: bool | None = None) -> str:
    """The fence highlighter on its own, one line per language."""
    if enabled is None:
        enabled = color_depth() != "none"
    from xli.ui.ansi import paint_span

    samples = [
        ("python", 'def f(x: int) -> str:  # возвращает строку'),
        ("bash", 'if [ -f "$HOME/.xli/config.json" ]; then echo ok; fi'),
        ("json", '{"model": "mistral-large", "temperature": 0.4, "stream": true}'),
        ("diff", '+ добавленная строка'),
        ("diff", '- удалённая строка'),
    ]
    out: list[str] = []
    for language, line in samples:
        cells: list[str] = []
        for text, style in highlight_line(line, language):
            cells.append(paint_span(text, style, enabled=enabled))
        out.append(f"{paint(language.ljust(8), None, dim=True, enabled=enabled)} {''.join(cells)}")
    return "\n".join(out)


def render_demo(
    width: int = 78,
    *,
    tick: int = 0,
    enabled: bool | None = None,
    only: str = "",
) -> str:
    """The whole demo, or one section of it, as a printable string."""
    if enabled is None:
        enabled = color_depth() != "none"
    parts: list[str] = []
    want = only.strip().lower()

    def add(name: str, body: str, title: str) -> None:
        if want and want != name:
            return
        if not want:
            parts.append(gradient_rule(width, tick=tick, enabled=enabled))
            parts.append(gradient_text(f" {title} ", *ACCENT_GLOW.stops[:2], True, enabled=enabled))
            parts.append(gradient_rule(width, tick=tick, enabled=enabled))
        parts.append(body)

    if not want or want == "banner":
        parts.append(render_banner(width, tick=tick, enabled=enabled))
    add("markdown", render_markdown_section(width, tick=tick, enabled=enabled), "markdown · glow")
    add("charts", render_charts_section(enabled=enabled), "charts · graphs")
    add("agents", render_agents_section(enabled=enabled), "agents · colours")
    add("report", render_report_section(tick=tick, enabled=enabled), "run report")
    add("graph", render_graph_section(enabled=enabled), "dependency graph")
    add("syntax", render_syntax_section(enabled=enabled), "syntax highlighting")
    return "\n".join(parts)


def animation_frames(count: int = 24, width: int = 78, *, enabled: bool = True) -> list[str]:
    """Frames of the animated banner, for `xli demo --animate` and for tests.

    Only the moving parts are re-rendered (the rule and the title), because a
    frame is one ``\\r``-prefixed line — repainting the whole document would
    flicker and cost far more than the effect is worth.
    """
    return [render_banner(width, tick=frame, enabled=enabled) for frame in range(count)]


__all__ = [
    "SAMPLE_MARKDOWN",
    "animation_frames",
    "render_agents_section",
    "render_banner",
    "render_charts_section",
    "render_demo",
    "render_graph_section",
    "render_markdown_section",
    "render_report_section",
    "render_syntax_section",
    "sections",
]
