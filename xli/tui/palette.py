#!/usr/bin/env python3
"""XLI TUI palette — the vocabulary every TUI module paints with.

Style *names* live here (not in ``widgets.py``) so that ``widgets`` and
``animations`` can both depend on them without depending on each other. That
was a real import cycle: ``animations`` imported ``widgets`` at module level
and ``widgets`` imported ``animations`` from inside ``separator_row`` — no
runtime failure, but the dependency scanner in ``xli graph`` reported it as a
cycle, and a cycle on the books is a cycle waiting to happen.

The names are shared with the markdown renderer and the CLI's ANSI painter, so
a ``heading`` means the same violet in all three front ends.
"""

from __future__ import annotations

# --------------------------------------------------------------------- styles
NORMAL = "normal"
DIM = "dim"
BOLD = "bold"
ACCENT = "accent"
GOOD = "good"
WARN = "warn"
BAD = "bad"
HEADING = "heading"
HEADING2 = "heading2"
HEADING3 = "heading3"
CODE = "code"
QUOTE = "quote"
LINK = "link"
ITALIC = "italic"
STRIKE = "strike"
#: The frame: the box around the interface. Violet-grey — present, but never
#: competing with the content it holds.
FRAME = "frame"
#: Bars and gauges inside panels.
GAUGE = "gauge"

#: Every style the painters must know about. ``xli/tui/app.py`` maps each one
#: to a curses attribute pair; anything missing falls back to ``normal``.
STYLE_NAMES: tuple[str, ...] = (
    NORMAL, DIM, BOLD, ACCENT, GOOD, WARN, BAD,
    HEADING, HEADING2, HEADING3, CODE, QUOTE, LINK, ITALIC, STRIKE,
    FRAME, GAUGE,
)

#: Style name -> xterm-256 foreground colour, or ``-1`` for the terminal
#: default. Violet is the accent family; heading levels step down it so
#: hierarchy survives on a monochrome-ish terminal without size changes.
#: ``code`` is a pale lilac and ``quote`` an olive-tinged grey, because a
#: reversed block (the old mapping) reads as a selection, not as code.
CURSES_PAIRS: dict[str, tuple[int, int]] = {
    DIM: (245, -1),
    BOLD: (255, -1),
    ACCENT: (135, -1),
    GOOD: (42, -1),
    WARN: (214, -1),
    BAD: (203, -1),
    HEADING: (177, -1),
    HEADING2: (141, -1),
    HEADING3: (252, -1),
    CODE: (187, -1),
    QUOTE: (103, -1),
    LINK: (81, -1),
    ITALIC: (146, -1),
    STRIKE: (244, -1),
    FRAME: (97, -1),
    GAUGE: (141, -1),
}

#: Which colour a permission mode is drawn in. The mode is the single most
#: consequential thing on screen -- it decides whether the agent may write --
#: so it gets its own colour rather than sharing the accent. ``auto`` is
#: deliberately alarming: "everything runs" should never look calm.
MODE_STYLE = {"readonly": ACCENT, "confirm": WARN, "auto": BAD}

#: Human words for the modes, in the interface language.
MODE_LABEL = {"readonly": ACCENT, "confirm": WARN, "auto": BAD}

#: Braille spinner frames. Braille dots occupy one cell in every terminal that
#: can draw them at all, so the bar does not jitter as the animation runs.
SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

#: Alternative frames, keader to the box-drawing family used everywhere else.
PIPE_SPINNER = ("│", "╱", "─", "╲")

#: Left-edge marker per transcript event. Together they form a spine you can
#: scan without reading the text: a solid bar is something said, a branch is a
#: tool, a vertical is its result. Sub-agent output gets its own marker so a
#: delegate's words are never mistaken for the main agent's.
GUTTER: dict[str, tuple[str, str]] = {
    "user": ("▌", ACCENT),
    "assistant": ("▌", GOOD),
    "tool_call": ("├", DIM),
    "tool_result": ("│", DIM),
    "repair": ("┊", WARN),
    "warning": ("┊", WARN),
    "error": ("▌", BAD),
    "step": ("·", DIM),
    "note": ("·", DIM),
    #: A `think` tool call: the agent talking to itself, worth showing but
    #: never to be mistaken for output or for speech to the user.
    "thought": ("∴", QUOTE),
    #: Sub-agent lifecycle: a small diamond marks "a delegate took over".
    "agent": ("◆", ACCENT),
    "agent_result": ("◇", DIM),
}

#: Width of the left gutter, in cells. Every transcript row reserves it so the
#: markers line up in a column and the text starts at the same place each time.
GUTTER_WIDTH = 2

#: Per-kind glyphs for tool calls, so the eye finds "this one touched a file"
#: before it reads the name. Keys are tool names; unknown tools get ``•``.
TOOL_GLYPH: dict[str, str] = {
    "read": "◍",
    "write": "✎",
    "edit": "✎",
    "bash": "$",
    "git": "⎇",
    "grep": "⌕",
    "glob": "⌕",
    "ls": "☰",
    "todo": "☑",
    "think": "∴",
    "web_search": "⌾",
    "fetch_page": "⇣",
    "fetch_pages": "⇣",
    "search_status": "◌",
    "mcp_call": "⚙",
    "mcp_list": "⚙",
    "outline": "❖",
    "apply_patch": "✚",
    "json_query": "{}",
    "repo_map": "▦",
    "dep_graph": "⛓",
}


def tool_glyph(name: str) -> str:
    """One cell that says what kind of tool this is."""
    return TOOL_GLYPH.get(name, "•")


#: A (text, style) pair — the atom every TUI row is built from.
Span = tuple[str, str]
#: A screen line: a list of spans.
Row = list[Span]


def span(text: str, style: str = NORMAL) -> Span:
    return (text, style)


def spinner_frame(tick: int, frames: tuple[str, ...] = SPINNER) -> str:
    """One frame, cycling. Negative and huge ticks both wrap safely."""
    return frames[tick % len(frames)]
