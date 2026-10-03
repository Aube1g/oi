#!/usr/bin/env python3
"""
XLI TUI layout — pure functions, no terminal required.

Everything that decides *what goes on which line* lives here and returns plain
data: a screen is a list of rows, a row is a list of (text, style) spans. That
makes the whole interface unit-testable with no pty and no curses, which is the
only way to keep a full-screen app honest in CI.

`xli/tui/app.py` is the thin curses adapter that paints these rows. Nothing
there decides layout, so a rendering bug is always reproducible from a test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from xli.ui.locale import t
from xli.ui.markdown import render_rows as md_rows
from xli.ui.summary import summarise_call
from xli.ui.text import display_width, json_dumps as _json_dumps, truncate as truncate_cells

# The style vocabulary, spinner and gutter markers live in `palette` so that
# `animations` can share them without importing this module (which used to be
# a cycle: animations -> widgets -> animations). They are re-exported here
# unchanged, because every caller and test already asks widgets for them.
from xli.tui.palette import (  # noqa: F401  (re-exported)
    ACCENT,
    BAD,
    BOLD,
    CODE,
    DIM,
    FRAME,
    GAUGE,
    GOOD,
    GUTTER,
    GUTTER_WIDTH,
    HEADING,
    HEADING2,
    HEADING3,
    ITALIC,
    LINK,
    MODE_LABEL,
    MODE_STYLE,
    NORMAL,
    QUOTE,
    SPINNER,
    STRIKE,
    WARN,
    span,
    spinner_frame,
    tool_glyph,
)
from xli.tui.frame import (  # noqa: F401  (re-exported)
    body_row,
    body_row as framed_body,
    bottom_row,
    fit as frame_fit,
    inner_width,
    row_width as frame_row_width,
    separator_row as labelled_separator,
    tab_strip,
    title_row,
)

Span = tuple[str, str]
Row = list[Span]


# ------------------------------------------------------------------- chrome
def extend_rule(row: Row, width: int, *, style: str = DIM) -> Row:
    """Append box-drawing fill to `row` so it spans exactly `width`.

    Takes a row of spans rather than a string, because the alternative --
    flattening to text first -- silently discards every style in it. The header
    did exactly that and the permission-mode badge, the one thing on screen that
    says whether the agent may write, came out the same grey as the rule.
    """
    if width <= 0:
        return []
    used = _row_width(row)
    fill = width - used
    if fill <= 0:
        return _fit(row, width)
    return _fit(list(row) + [("─" * fill, style)], width)


def _rule(width: int, *, left: str = "", right: str = "", style: str = DIM) -> Row:
    """A horizontal rule with text at each end, filled with box-drawing."""
    if width <= 0:
        return []
    taken = display_width(left) + display_width(right)
    fill = width - taken
    if fill < 1:
        return _fit([(left + right, style)], width)
    if fill == 1:
        return _fit([(left, style), ("─", style), (right, style)], width)
    left_pad = " " if left else ""
    right_pad = " " if right else ""
    line = fill - display_width(left_pad) - display_width(right_pad)
    if line < 1:
        return _fit([(left + right, style)], width)
    return _fit(
        [(left, style), (left_pad, style), ("─" * line, style), (right_pad, style), (right, style)],
        width,
    )


def header_rows(
    width: int,
    *,
    model: str,
    provider: str,
    mode: str,
    session: str = "",
    kernel: str = "",
    busy: bool = False,
    activity: str = "",
    tick: int = 0,
) -> list[Row]:
    """The top of the frame: identity on the left, live state on the right.

    Row one is the title band — ``XLI``, the provider, the session, and the
    pulse that says the program is alive. Row two is the state strip: mode,
    kernel, and either the current activity (with the spinner) or a summary
    that it is waiting. Two rows are enough: a header that grows eats the
    conversation, and the conversation is the point.
    """
    mode_style = MODE_STYLE.get(mode, ACCENT)
    pulse = spinner_frame(tick) if busy else "●"
    pulse_style = ACCENT if busy else GOOD

    title: Row = [span("XLI", BOLD), span(" ─ ", FRAME), span(provider, DIM)]
    if model:
        title += [span("/", FRAME), span(model, DIM)]
    right: Row = [span(f"{pulse} ", pulse_style), span(session[:18] or "—", DIM)]
    first = title_row(width, title, right)

    left: Row = [span(f" {mode} ", mode_style)]
    if kernel:
        left += [span(" ─ ", FRAME), span(f"kernel:{kernel} ", DIM)]
    if busy and activity:
        left += [span(" ", FRAME), span(f"{activity} ", ACCENT)]
    second = body_row(left, width, right="│")
    return [first, second]


def _justify(rows: list[Row], width: int) -> list[Row]:
    """Pad/truncate each row to exactly `width` columns."""
    out: list[Row] = []
    for row in rows:
        out.append(_fit(row, width))
    return out


def _fit(row: Row, width: int) -> Row:
    """Truncate a row to `width` cells, then pad with blanks.

    Measured in terminal cells, not characters: a wide glyph occupies two, so
    counting len() let the row overrun and the frame stepped out of alignment
    on every redraw.
    """
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


def _row_width(row: Row) -> int:
    return sum(display_width(text) for text, _ in row)


def truncate_left(text: str, width: int) -> str:
    """Keep the *end* of `text`, prefixed with an ellipsis.

    For the input line, where the newest characters are what the user is
    looking at, so scrolling has to drop from the left.
    """
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    budget = width - display_width("…")
    if budget <= 0:
        return "…"
    chars = list(text)
    out: list[str] = []
    used = 0
    for char in reversed(chars):
        from xli.ui.text import char_width

        step = char_width(char)
        if used + step > budget:
            break
        out.append(char)
        used += step
    return "…" + "".join(reversed(out))


# ---------------------------------------------------------------- transcript
#: Width of the left gutter, in cells. Every transcript row reserves it so the
#: markers line up in a column and the text starts at the same place each time.
GUTTER_WIDTH = 2


def _gutter(kind: str) -> Row:
    """The left-edge marker for an event kind."""
    mark, style = GUTTER.get(kind, (" ", DIM))
    return [span(f"{mark} ", style)]


def agent_style_of(payload: dict[str, Any]) -> str:
    """The colour the speaker is drawn in.

    The main agent is the house violet; a delegate gets its own colour, derived
    from its name so it is the same colour tomorrow. Two delegates never share
    a colour by accident, which is what makes a transcript readable when the
    reviewer and the test-writer talk in turn.
    """
    agent_id = str(payload.get("agent_id") or "main")
    name = str(payload.get("agent_name") or "")
    if agent_id == "main" or not name:
        return ACCENT
    from xli.ui.agents import agent_color, agent_hint_for

    return agent_color(name, payload.get("agent_hint") or agent_hint_for(name))


def transcript_row(
    kind: str,
    payload: dict[str, Any],
    width: int,
    *,
    tick: int = 0,
) -> list[Row]:
    """Render one agent event into framed screen rows.

    The spine is consistent: ``▌`` is somebody speaking, ``├`` is a tool being
    called, ``│`` is its result, ``◆``/``◇`` is a delegate arriving or leaving.
    A delegate's lines are drawn in that delegate's own colour, so "who said
    this" is answered by the colour before the text is read.
    """
    inner = inner_width(width)
    gutter = _gutter(kind)

    if kind == "user":
        line = gutter + [span(t("tui_you"), ACCENT), span(str(payload.get("text", "")))]
        return [body_row(row, width) for row in _wrap(line, inner)]

    if kind == "assistant":
        text = str(payload.get("text", "")).strip()
        if not text:
            return []
        colour = agent_style_of(payload)
        name = str(payload.get("agent_name") or "xli")
        badge = "xli" if name in ("", "xli") else name
        body_width = max(20, inner - GUTTER_WIDTH - 4)
        out: list[Row] = [
            gutter + [span("◆ ", colour), span(badge, colour), span(" — ", DIM)]
        ]
        for row in md_rows(text, body_width):
            out.append([span(" " * (GUTTER_WIDTH + 1), NORMAL)] + row)
        return [body_row(row, width) for row in out]

    if kind == "tool_call":
        name = str(payload.get("name", ""))
        args = payload.get("args") or {}
        colour = agent_style_of(payload)
        if name == "think":
            thought = str(args.get("thought", "")).strip()
            rows = _wrap(_gutter("thought") + [span(thought, ITALIC)], inner)
            return [body_row(row, width) for row in rows]
        if name == "delegate":
            target = str(args.get("agent_name", "?"))
            from xli.ui.agents import agent_color, agent_hint_for

            target_colour = agent_color(target, agent_hint_for(target))
            line: Row = [
                span("◆ ", colour),
                span("делегирую → ", DIM),
                span(target, target_colour),
            ]
            task = str(args.get("task", "")).strip()
            if task:
                line += [span("  " + task[:80], DIM)]
            return [body_row(row, width) for row in _wrap(line, inner)]
        glyph = tool_glyph(name)
        line = _gutter(kind) + [
            span(f"{glyph} ", colour),
            span(name, colour),
            span(f" {summarise_call(name, args)}", DIM),
        ]
        return [body_row(row, width) for row in _wrap(line, inner)]

    if kind == "tool_result":
        ok = bool(payload.get("ok"))
        colour = GOOD if ok else BAD
        mark = "✓" if ok else "✗"
        ms = payload.get("duration_ms")
        summary = str(payload.get("summary", ""))
        line: Row = _gutter(kind) + [span(f"{mark} ", colour), span(summary, DIM if ok else NORMAL)]
        if isinstance(ms, (int, float)) and ms >= 50:
            timing = (
                t("unit_s", n=f"{ms / 1000:.1f}".replace(".", ","))
                if ms >= 1000
                else t("unit_ms", n=f"{ms:.0f}")
            )
            line += [span(f"  {timing}", WARN if ms >= 2000 else DIM)]
        return [body_row(row, width) for row in _wrap(line, inner)]

    if kind in ("agent",):
        phase = str(payload.get("phase", ""))
        name = str(payload.get("agent_name") or payload.get("name") or "?")
        if str(payload.get("agent_id") or "main") == "main":
            # The main agent starting and finishing is already said by the
            # step divider and the closing note; repeating it as a delegate
            # line would make the transcript look like it had three actors.
            return []
        colour = agent_style_of(payload)
        if phase == "end":
            from xli.ui.locale import steps_word

            reason = str(payload.get("stopped_reason", "?"))
            steps = int(payload.get("steps", 0) or 0)
            seconds = float(payload.get("seconds", 0) or 0)
            line = [
                span("◇ ", colour),
                span(name, colour),
                span(
                    "  " + t("tui_agent_done", steps=steps_word(steps), seconds=f"{seconds:.1f} с"),
                    DIM,
                ),
            ]
        else:
            line = [
                span("◆ ", colour),
                span(name, colour),
                span(f"  {str(payload.get('task', ''))[:80]}", DIM),
            ]
        return [body_row(row, width) for row in _wrap(line, inner)]

    if kind == "repair":
        return [body_row(row, width) for row in _wrap(_gutter(kind) + [span(str(payload.get("detail", "")), WARN)], inner)]

    if kind == "warning":
        return [body_row(row, width) for row in _wrap(_gutter(kind) + [span(str(payload.get("message", "")), WARN)], inner)]

    if kind == "error":
        return [body_row(row, width) for row in _wrap(_gutter(kind) + [span(str(payload.get("message", "")), BAD)], inner)]

    if kind == "step":
        # A step is a boundary in the conversation, so it is drawn as one: a
        # labelled divider rather than one more dim line lost in the flow.
        text = t("tui_step", index=payload.get("index"), max_steps=payload.get("max_steps"))
        return [labelled_separator(width, left=[span(text, DIM)], style=FRAME)]

    if kind == "note":
        return [body_row(row, width) for row in _wrap(_gutter(kind) + [span(str(payload.get("text", "")), DIM)], inner)]

    return []


def _compact_args(args: dict[str, Any], limit: int = 90) -> str:
    import json

    try:
        text = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(args)
    return text if display_width(text) <= limit else truncate_cells(text, limit)


def _wrap(row: Row, width: int) -> list[Row]:
    """Word-wrap a row across `width`, honouring explicit newlines."""
    if width <= 0:
        return [[]]

    pieces: list[Span] = []
    for text, style in row:
        parts = text.split("\n")
        for index, part in enumerate(parts):
            if index:
                pieces.append(("\n", style))
            pieces.append((part, style))

    rows: list[Row] = []
    current: Row = []
    used = 0

    for text, style in pieces:
        if text == "\n":
            rows.append(current)
            current, used = [], 0
            continue
        for word in _split_words(text):
            word_len = display_width(word)
            if used + word_len > width and current:
                rows.append(current)
                current, used = [], 0
                # Do not carry a leading space onto the new line.
                if word == " ":
                    continue
            current.append((word, style))
            used += word_len

    rows.append(current)
    return [_fit(row, width) for row in rows]


def _split_words(text: str) -> list[str]:
    """Split into words keeping separators, so wrapping is lossless."""
    if not text:
        return []
    words: list[str] = []
    buffer = ""
    for char in text:
        if char == " ":
            if buffer:
                words.append(buffer)
                buffer = ""
            words.append(" ")
        else:
            buffer += char
    if buffer:
        words.append(buffer)
    return words


# ------------------------------------------------------------------ statusbar
def status_row(
    width: int,
    *,
    busy: bool = False,
    hint: str = "",
    counters: dict[str, Any] | None = None,
    tick: int = 0,
    mode: str = "",
    activity: str = "",
    extra: str = "",
) -> Row:
    """The bottom edge: what is happening, how much has happened, how to leave.

    The keys win when space runs short. A counter can be dropped without
    costing the user anything they cannot find elsewhere; the way out of the
    program cannot. So the hint is placed first (right-aligned) and the left
    half is truncated into whatever is left.
    """
    counters = counters or {}
    keys = hint or t("tui_keys")
    tail = f" {keys} " if keys else ""
    tail_cells = display_width(tail)

    if busy:
        state: Row = [
            span(f" {spinner_frame(tick)} ", ACCENT),
            span(activity or t("tui_working"), ACCENT),
        ]
    else:
        state = [span(" ● ", GOOD), span(t("tui_ready"), GOOD)]
    if mode:
        state += [span(" · ", DIM), span(mode, MODE_STYLE.get(mode, DIM))]

    bits: list[str] = []
    for key in ("steps", "tools", "errors", "tokens"):
        if key in counters:
            bits.append(t(f"tui_{key}", n=counters[key]))
    if extra:
        bits.append(extra)
    if bits:
        state += [span("  ", NORMAL), span(" · ".join(bits), DIM)]

    available = width - 4 - tail_cells - 1  # borders, corners, the tail's spaces
    left = state
    if frame_row_width(left) > available > 0:
        trimmed: Row = []
        used = 0
        for text, style in left:
            room = available - used
            if room <= 0:
                break
            if display_width(text) > room:
                trimmed.append((truncate_cells(text, room), style))
                used = available
                break
            trimmed.append((text, style))
            used += display_width(text)
        left = trimmed
    filler = max(0, width - 4 - frame_row_width(left) - tail_cells)
    return bottom_row(width, list(left) + [span(" " * filler, NORMAL), span(tail, DIM)])


def input_row(
    width: int,
    prompt: str,
    text: str,
    *,
    cursor_visible: bool = True,
    mode: str = "",
    hint: str = "",
) -> Row:
    """The composition line, inside the frame.

    The prompt takes the mode's colour, so a session switched to readonly looks
    different at the point where the user is typing — the moment the
    distinction matters. With an empty buffer the hint shows where to type.
    """
    inner = inner_width(width)
    prefix = span(prompt, MODE_STYLE.get(mode, ACCENT))
    available = inner - display_width(prompt) - 1
    if text:
        body_span = span(text if display_width(text) <= available else truncate_left(text, available))
    elif hint:
        body_span = span(truncate_cells(hint, available), DIM)
    else:
        body_span = span("")
    row: Row = [prefix, body_span]
    if cursor_visible and frame_row_width(row) < available:
        row.append(span("▏", ACCENT))
    return body_row(row, width)


def separator_row(width: int, *, style: str = ACCENT, tick: int = 0) -> Row:
    """Thin purple separator with a travelling bright spot.

    Uses :func:`xli.tui.animations.animated_separator` when ``tick > 0``,
    otherwise falls back to a static thin line in *style*.
    """
    if width <= 0:
        return []
    if tick > 0:
        from xli.tui.animations import animated_separator
        spans = animated_separator(width, tick, style=style)
        return _fit(spans, width)
    return _fit([("─" * width, style)], width)


# --------------------------------------------------------------------- layout
@dataclass
class Layout:
    """Where each region starts, given a terminal size."""

    width: int
    height: int
    header_lines: int = 2
    #: Two, not one: the rule above the composition line is part of the region.
    #: Painting both rows while reserving one made the status bar overwrite the
    #: line the user was typing on.
    input_lines: int = 2
    status_lines: int = 1

    @property
    def body_top(self) -> int:
        return self.header_lines

    @property
    def body_height(self) -> int:
        return max(
            1, self.height - self.header_lines - self.input_lines - self.status_lines
        )

    @property
    def body_bottom(self) -> int:
        return self.body_top + self.body_height

    @property
    def input_top(self) -> int:
        return self.body_bottom

    @property
    def status_top(self) -> int:
        return self.input_top + self.input_lines

    def is_usable(self) -> bool:
        return self.width >= 20 and self.height >= 8


def make_layout(width: int, height: int) -> Layout:
    return Layout(width=width, height=height)


def visible_window(rows: list[Row], height: int, scroll: int) -> tuple[list[Row], int]:
    """Slice `rows` to what fits, honouring a scroll offset from the bottom.

    scroll=0 means "pinned to the newest line", which is the normal state; a
    positive scroll walks backwards into history.
    """
    if not rows:
        return [], 0
    total = len(rows)
    start = max(0, total - height - max(0, scroll))
    end = min(total, start + height)
    return rows[start:end], total - end


def scroll_limit(total_rows: int, height: int) -> int:
    return max(0, total_rows - height)


# ------------------------------------------------------------------- approvals
def approval_rows(tool: str, args: dict[str, Any], reason: str, width: int) -> list[Row]:
    """The modal asking permission for a mutating tool call.

    Drawn as its own box so it cannot be mistaken for conversation, and the
    keys are on the box's last line: "y/n/a" is the only thing the user needs
    to read while it is up.
    """
    inner = inner_width(width)
    body: list[Row] = [
        [span("⚠ ", WARN), span(t("tui_approve", tool=tool), BOLD)],
        [span(reason, DIM)],
    ]
    for line in _wrap([span(summarise_call(tool, args), NORMAL)], inner - 2):
        body.append(line)
    rows: list[Row] = [title_row(width, [span("подтверждение", WARN)])]
    rows += [body_row(row, width) for row in body]
    rows.append(_frame_keys(width, t("tui_approve_keys")))
    return rows


def _frame_keys(width: int, keys: str) -> Row:
    return bottom_row(width, [span(keys.strip(), ACCENT)])


# json_dumps lives in xli.ui.text so xli.ui.summary can use it without
# importing this module back (summary → widgets closed an import cycle). The
# re-export keeps the historical widgets.json_dumps name working.
json_dumps = _json_dumps


# ------------------------------------------------------------------- help pane
HELP_TEXT = """\
XLI — keys
  Enter        send the line
  Up / Down    walk input history
  PgUp / PgDn  scroll the transcript
  Home / End   jump to top / bottom of the transcript
  Tab          next side panel (plan → graph → agents → stats)
  1..4         open one panel · Esc close it
  ^L           redraw
  ^C           quit
  /help        list slash commands

slash commands
  /help        this pane
  /tools       list available tools
  /plan        task list the agent is working through
  /graph       what depends on what (module impact)
  /agents      who is working, and in which colour
  /stats       steps, tools, errors, time per step
  /mode auto|confirm|readonly
  /model NAME  switch model
  /session     show the session id
  /clear       clear the transcript
  /quit        exit
"""

HELP_TEXT_RU = """\
XLI — клавиши
  Enter        отправить строку
  Up / Down    история ввода
  PgUp / PgDn  листать транскрипт
  Home / End   в начало / в конец транскрипта
  Tab          следующая панель (план → граф → агенты → статистика)
  1..4         открыть панель · Esc закрыть
  ^L           перерисовать
  ^C           выход
  /help        список slash-команд

slash-команды
  /help        эта панель
  /tools       доступные инструменты
  /plan        список задач, которые делает агент
  /graph       что от чего зависит (влияние модулей)
  /agents      кто работает и каким цветом
  /stats       шаги, инструменты, ошибки, время на шаг
  /mode auto|confirm|readonly
  /model NAME  сменить модель
  /session     показать id сессии
  /clear       очистить транскрипт
  /quit        выход
"""


def help_rows(width: int) -> list[Row]:
    from xli.ui.locale import lang

    text = HELP_TEXT_RU if lang() == "ru" else HELP_TEXT
    rows: list[Row] = []
    for line in text.splitlines():
        rows.append(_fit([span(line, DIM if line.startswith(" ") else BOLD)], width))
    return rows
