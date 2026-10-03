#!/usr/bin/env python3
"""
XLI TUI — full-screen interface (curses).

The adapter is deliberately dumb. `xli.tui.widgets` decides what belongs on
which line; this file only:

  * owns the terminal (raw mode, no echo, restore on exit)
  * maps bytes from the keyboard to actions
  * paints the rows it is handed

Slash commands and tool approvals are handled here because they are pure input
concerns — neither needs to know anything about curses.

The agent runs on the same event loop as the UI, so a long tool call cannot
freeze input: each turn yields back to the screen between awaits.
"""

from __future__ import annotations

import asyncio
import curses
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from xli.ui.locale import t
from xli.ui.text import display_width
from xli.tui.widgets import (  # noqa: F401  (re-exported for tests)
    Row,
    approval_rows,
    header_rows,
    help_rows,
    input_row,
    make_layout,
    scroll_limit,
    status_row,
    transcript_row,
    visible_window,
)

from xli.tui import panels
from xli.tui.frame import separator_row as frame_separator

KEY_UP = 259
KEY_DOWN = 258


@dataclass
class TuiState:
    rows: list[Row] = field(default_factory=list)
    scroll: int = 0
    buffer: str = ""
    history: list[str] = field(default_factory=list)
    history_index: int = -1
    busy: bool = False
    show_help: bool = False
    counters: dict[str, Any] = field(default_factory=dict)
    approval: dict[str, Any] | None = None
    approval_future: asyncio.Future | None = None
    allow_all: bool = False
    model: str = ""
    provider: str = ""
    mode: str = "confirm"
    session_id: str = ""
    kernel: str = ""
    #: Advances on every repaint so the spinner moves. A static "working…" on a
    #: request that has hung is indistinguishable from one about to finish.
    tick: int = 0
    #: Which side panel is open, if any (Tab cycles, Esc closes).
    panel_open: bool = False
    panel_index: int = 0
    #: What the agent is doing *right now*, for the status bar.
    activity: str = ""
    #: Everything the panels need, kept as it happens rather than recomputed.
    started: float = 0.0
    step_seconds: list[float] = field(default_factory=list)
    tool_counts: dict[str, int] = field(default_factory=dict)
    delegates: dict[str, dict] = field(default_factory=dict)
    plan: list = field(default_factory=list)
    graph_rows: list = field(default_factory=list)
    graph_loading: bool = False


STYLE_ATTRS = (
    "normal", "dim", "bold", "accent", "good", "warn", "bad",
    "heading", "heading2", "heading3", "code", "quote", "link",
    "italic", "strike",
)


def _init_colors() -> dict[str, int]:
    """Map style names to curses attributes; mono terminals degrade cleanly.

    Every style the markdown renderer can emit must be present, otherwise it
    paints unstyled. Attributes are added on top of the colour pair, so a
    terminal without colour still gets bold/reverse/underline.
    """
    if not curses.has_colors():
        mapping = dict.fromkeys(STYLE_ATTRS, curses.A_NORMAL)
        mapping["bold"] |= curses.A_BOLD
        mapping["heading"] = curses.A_BOLD
        mapping["heading2"] = curses.A_BOLD
        mapping["heading3"] = curses.A_BOLD
        mapping["code"] = curses.A_REVERSE
        mapping["link"] = curses.A_UNDERLINE
        return mapping

    curses.start_color()
    curses.use_default_colors()
    # 256-colour palette; the fallbacks below keep it usable on 8-colour too.
    # The accent is violet, not cyan — cyan reads as "link / info" in most
    # terminals, while violet is unmistakably *this app's* colour. Heading
    # levels step down the violet family so hierarchy survives without size.
    pairs = {
        "dim": (245, -1),
        "bold": (255, -1),
        "accent": (135, -1),
        "good": (42, -1),
        "warn": (214, -1),
        "bad": (203, -1),
        "heading": (177, -1),
        "heading2": (141, -1),
        "heading3": (252, -1),
        "code": (187, -1),
        "quote": (103, -1),
        "link": (81, -1),
        "italic": (146, -1),
        "strike": (244, -1),
    }
    mapping = {"normal": curses.A_NORMAL}
    for index, (name, (fg, bg)) in enumerate(pairs.items(), start=1):
        try:
            curses.init_pair(index, fg, bg)
            mapping[name] = curses.color_pair(index)
        except curses.error:
            mapping[name] = curses.A_NORMAL
    mapping["bold"] |= curses.A_BOLD
    mapping["heading"] |= curses.A_BOLD
    mapping["heading2"] |= curses.A_BOLD
    mapping["heading3"] |= curses.A_BOLD
    mapping["code"] |= curses.A_REVERSE
    mapping["quote"] |= curses.A_ITALIC if hasattr(curses, "A_ITALIC") else 0
    mapping["link"] |= curses.A_UNDERLINE
    mapping["strike"] |= curses.A_STANDOUT
    return mapping


class Tui:
    """Curses front end for the agent."""

    def __init__(self, config, *, registry=None, policy=None, provider=None):
        self.config = config
        self.registry = registry
        self.policy = policy
        self.provider = provider
        self.state = TuiState(
            model=str(config.get("provider.model")),
            provider=str(config.get("provider")),
            mode=config.permission_mode(),
            kernel="cython" if config.kernel_enabled() else "python",
        )
        self.screen = None
        self.attrs: dict[str, int] = {}
        self._agent_task: asyncio.Task | None = None
        self._tick: int = 0  # animation frame counter
        self._pairs_used: int = 0
        self._pending: list = []
        self._debug = os.environ.get("XLI_TUI_DEBUG", "")
        #: Set by the SIGINT handler. The terminal driver turns ^C into a
        #: signal, not a key, and inside a running asyncio loop that signal was
        #: being swallowed: the interface stayed up and stopped responding.
        self._interrupted = False

        # One session per TUI run, kept across tasks, so reopening the app
        # shows the conversation so far instead of a blank screen that
        # pretends nothing ever happened.
        from pathlib import Path

        from xli.session import Session

        try:
            self.session = Session(root=Path.cwd())
        except Exception:  # noqa: BLE001 - a broken session store must not kill the UI
            self.session = None
        if self.session is not None:
            self.state.session_id = self.session.session_id

    def _seed_history(self) -> None:
        """Paint the session so far into the transcript on startup.

        A blank screen that pretends nothing ever happened is exactly the
        complaint "where are my previous messages" — the history is on disk,
        so show it.
        """
        if self.session is None or self.state.rows:
            return
        width = self._width()
        events = getattr(self.session, "events", []) or []
        for event in events[-200:]:
            kind = getattr(event, "kind", "")
            data = getattr(event, "data", {}) or {}
            text = str(data.get("content", "")).strip()
            if not text:
                continue
            if kind == "user":
                self.state.rows.extend(transcript_row("user", {"text": text}, width))
            elif kind == "assistant":
                self.state.rows.extend(transcript_row("assistant", {"text": text}, width))
        if self.state.rows:
            self.state.rows.extend(
                transcript_row("note", {"text": t("tui_history_note")}, width)
            )

    # ------------------------------------------------------------------ paint
    def draw(self) -> None:
        if self.screen is None:
            return
        self._tick += 1
        height, raw_width = self.screen.getmaxyx()
        # One column short of the terminal: curses cannot write the last cell
        # of a line (it would wrap), so a frame drawn to the exact width loses
        # its right border on every line.
        width = max(20, raw_width - 1)
        layout = make_layout(width, height)
        if not layout.is_usable():
            self._draw_too_small(width, height)
            return

        self.screen.erase()
        self._paint_rows(0, header_rows(
            width,
            model=self.state.model,
            provider=self.state.provider,
            mode=self.state.mode,
            session=self.state.session_id,
            kernel=self.state.kernel,
            busy=self.state.busy,
            activity=self.state.activity,
            tick=self._tick,
        ))

        panel_height = self._panel_height(layout)
        body_height = max(1, layout.body_height - panel_height)

        if self.state.show_help:
            body = help_rows(width)
        else:
            body = self.state.rows

        window, above = visible_window(body, body_height, self.state.scroll)
        self._paint_rows(layout.body_top, window)
        if panel_height:
            self._paint_panel(layout.body_top + body_height, panel_height, layout, width)

        if self.state.approval is not None:
            modal = approval_rows(
                self.state.approval["tool"],
                self.state.approval["args"],
                self.state.approval["reason"],
                width,
            )
            top = max(layout.body_top, layout.input_top - len(modal))
            self._paint_rows(top, modal)

        # The rule above the composition line animates while the agent works:
        # a motionless screen during a long tool call is indistinguishable from
        # a hung one.
        self._paint_rows(
            layout.input_top,
            [
                self._animated_rule(width),
                input_row(
                    width,
                    " ❯ ",
                    self.state.buffer,
                    mode=self.state.mode,
                    hint="" if (self.state.buffer or self.state.busy) else t("tui_hint"),
                ),
            ],
        )
        self._paint_rows(
            layout.status_top,
            [
                status_row(
                    width,
                    busy=self.state.busy,
                    counters=self.state.counters,
                    hint="",
                    tick=self._tick,
                    mode=self.state.mode,
                    activity=self.state.activity,
                    extra=self._scroll_note(len(body), body_height, above),
                )
            ],
        )
        self.state.tick += 1
        self.screen.refresh()

    def _scroll_note(self, total: int, visible: int, above: int) -> str:
        """`строка 12/140` while scrolled back — a scrollbar in text form."""
        if self.state.scroll <= 0 or total <= visible:
            return ""
        position = max(1, total - visible - above)
        return t("tui_scroll", pos=position, total=total)

    def _animated_rule(self, width: int) -> Row:
        """The frame divider above the input, with a travelling highlight."""
        from xli.tui.animations import animated_separator

        from xli.ui.text import display_width

        spans = animated_separator(width - 2, self._tick, style="frame")
        used = sum(display_width(text) for text, _ in spans)
        if used < width - 2:
            spans.append(("─" * (width - 2 - used), "frame"))
        return [("├", "frame"), *spans, ("┤", "frame")]

    def _panel_height(self, layout) -> int:
        if not self.state.panel_open:
            return 0
        # Enough to be useful, never more than half the body: the transcript is
        # the main event, the panel is a reference.
        return max(6, min(12, layout.body_height // 2))

    def _paint_panel(self, top: int, height: int, layout, width: int) -> None:
        state = panels.PanelState(
            plan=list(self.state.plan),
            agents=self._agent_cards(),
            graph_rows=list(self.state.graph_rows),
            counters=dict(self.state.counters),
            step_seconds=list(self.state.step_seconds),
            tool_counts=dict(self.state.tool_counts),
            started=self.state.started,
            now=time.monotonic(),
            activity=self.state.activity,
        )
        name = panels.PANELS[self.state.panel_index][0]
        rows = panels.panel_rows(
            name, state, width, height, active=self.state.panel_index, tick=self._tick
        )
        self._paint_rows(top, rows)

    def _agent_cards(self) -> list:
        """One card per delegate seen this session, newest state each repaint."""
        cards = []
        now = time.monotonic()
        for name, info in sorted(self.state.delegates.items()):
            started = info.get("started") or now
            finished = info.get("finished")
            end = finished or now
            cards.append(
                panels.AgentCard(
                    name=name,
                    colour=info.get("colour", ""),
                    status="готово" if finished else "работает",
                    steps=int(info.get("steps", 0)),
                    seconds=max(0.0, end - started),
                    task=str(info.get("task", "")),
                )
            )
        return cards

    def _draw_too_small(self, width: int, height: int) -> None:
        self.screen.erase()
        message = t("tui_small", width=width, height=height)
        self.screen.addnstr(0, 0, message, max(0, width - 1))
        self.screen.refresh()

    def _attr_for(self, style: str) -> int:
        """Resolve a style name — or a `#RRGGBB` — to a curses attribute.

        Delegates are coloured from their name, so the front end is handed hex
        strings it has never seen before. Allocating a colour pair on first use
        (and caching it) is what lets every sub-agent have its own colour
        instead of the three the palette could be bothered to pre-define.
        """
        cached = self.attrs.get(style)
        if cached is not None:
            return cached
        attr = curses.A_NORMAL
        if style.startswith("#") and len(style) == 7 and curses.has_colors():
            try:
                from xli.ui.gradient import hex_to_rgb, xterm256_index

                index = xterm256_index(hex_to_rgb(style))
                pair = 100 + (self._pairs_used % 60)
                self._pairs_used += 1
                curses.init_pair(pair, index, -1)
                attr = curses.color_pair(pair)
            except Exception:  # noqa: BLE001 - a bad colour must not kill the UI
                attr = curses.A_NORMAL
        self.attrs[style] = attr
        return attr

    def _paint_rows(self, top: int, rows: list[Row]) -> None:
        height, width = self.screen.getmaxyx()
        for offset, row in enumerate(rows):
            line = top + offset
            if line >= height:
                break
            column = 0
            for text, style in row:
                if column >= width - 1:
                    break
                attr = self._attr_for(style)
                try:
                    self.screen.addnstr(line, column, text, width - column - 1, attr)
                except curses.error:
                    pass  # writing the bottom-right cell always raises; ignore it
                # Cells, not bytes and not codepoints: a wide glyph occupies
                # two columns, and counting it as one walks every later span
                # out of alignment.
                column += display_width(text)

    # ------------------------------------------------------------------ input
    #: Escape sequences the app understands, by what follows ESC. Curses does
    #: not always decode these: with a short `timeout()` it can hand back a
    #: bare ESC and then the rest as ordinary characters, which is how the up
    #: arrow ended up typed into the input line as "[A".
    ESCAPE_SEQUENCES: dict[str, int] = {
        "[A": KEY_UP,
        "[B": KEY_DOWN,
        "OA": KEY_UP,
        "OB": KEY_DOWN,
        "[5~": curses.KEY_PPAGE,
        "[6~": curses.KEY_NPAGE,
        "[H": curses.KEY_HOME,
        "[F": curses.KEY_END,
        "OH": curses.KEY_HOME,
        "OF": curses.KEY_END,
        "[1~": curses.KEY_HOME,
        "[4~": curses.KEY_END,
        "[3~": curses.KEY_DC,
    }

    #: Escape sequences this app understands, keyed by their bytes.
    ESCAPE_SEQUENCES: dict[bytes, int] = {
        b"\x1b[A": KEY_UP,
        b"\x1b[B": KEY_DOWN,
        b"\x1bOA": KEY_UP,
        b"\x1bOB": KEY_DOWN,
        b"\x1b[5~": curses.KEY_PPAGE,
        b"\x1b[6~": curses.KEY_NPAGE,
        b"\x1b[H": curses.KEY_HOME,
        b"\x1b[F": curses.KEY_END,
        b"\x1bOH": curses.KEY_HOME,
        b"\x1bOF": curses.KEY_END,
        b"\x1b[1~": curses.KEY_HOME,
        b"\x1b[4~": curses.KEY_END,
        b"\x1b[3~": curses.KEY_DC,
    }

    def _read_char(self):
        """One keypress: a str for text, an int for a function key.

        This reads bytes from the terminal instead of calling `get_wch()`,
        which sounds gratuitous for a curses program and is not. `get_wch()`
        *sometimes* decodes arrow keys and *sometimes* eats a lone Escape while
        it waits for a sequence that never comes — so Esc, the key that closes
        a panel, worked or did not depending on timing. Reading the bytes makes
        the decoding explicit: escape sequences are matched as bytes, text is
        decoded as UTF-8 (which is also why Cyrillic stopped arriving as
        mojibake), and everything else is one character.
        """
        if self.screen is None:
            return -1
        if self._pending:
            return self._pending.pop(0)

        data = self._read_bytes()
        if not data:
            return -1

        if data == b"\x03":
            return 3
        if data == b"\x04":
            return 4
        if data == b"\x1b":
            return "\x1b"
        if data.startswith(b"\x1b"):
            sequence = self._gather_escape(data)
            return self.ESCAPE_SEQUENCES.get(sequence, -1)

        text = self._decode_utf8(data)
        for char in text[1:]:
            self._pending.append(char)
        return text[0] if text else -1

    def _read_bytes(self, timeout: float = 0.05) -> bytes:
        """One chunk from the terminal, or b"" after `timeout`."""
        import select

        fd = sys.stdin.fileno()
        try:
            ready, _, _ = select.select([fd], [], [], timeout)
        except (OSError, ValueError):
            return b""
        if not ready:
            return b""
        try:
            return os.read(fd, 64)
        except OSError:
            return b""

    def _gather_escape(self, first: bytes) -> bytes:
        """Complete an escape sequence, given its first byte(s)."""
        sequence = first
        deadline = time.monotonic() + 0.05
        while time.monotonic() < deadline:
            if sequence.endswith(b"~") or (len(sequence) > 1 and sequence[-1:].isalpha()):
                break
            more = self._read_bytes(deadline - time.monotonic())
            if not more:
                break
            sequence += more
        if len(sequence) > 1 and not sequence[1:2].isalpha() and not sequence[1:2] == b"[":
            # ESC followed by an ordinary character: two separate keys.
            for byte in sequence[1:]:
                self._pending.append(bytes([byte]).decode("utf-8", "replace"))
            return b"\x1b"
        return sequence[:8]

    def _decode_utf8(self, data: bytes) -> str:
        """Decode a chunk, pulling more bytes while a character is incomplete."""
        while True:
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError as exc:
                if exc.reason != "unexpected end of data":
                    return data.decode("utf-8", "replace")
                more = self._read_bytes()
                if not more:
                    return data.decode("utf-8", "replace")
                data += more

    def _handle_key(self, key) -> bool:
        """Return False to quit. `key` is a str or an int."""
        state = self.state

        if state.approval is not None:
            return self._handle_approval_key(key)

        # Text arrives as a one-character string; keys as an int.
        if isinstance(key, str):
            if key in ("\n", "\r"):
                self._submit()
            elif key == "\t":
                self._cycle_panel()
            elif key == "\x1b":
                if self.state.panel_open:
                    self.state.panel_open = False
                    self.draw()
            elif key in ("\x03", "\x04"):  # ^C / ^D
                return False
            elif key == "\x0c":
                self.draw()
            elif key == "\x15":
                state.buffer = ""
            elif key in ("\x08", "\x7f"):
                state.buffer = state.buffer[:-1]
            elif key >= " ":
                state.buffer += key
            return True

        if key in (3, 4):  # ^C / ^D
            return False
        if key == 12:  # ^L
            self.draw()
            return True
        if key == 9:  # Tab cycles the side panels
            self._cycle_panel()
            return True
        if key == 27:  # Esc closes them
            if self.state.panel_open:
                self.state.panel_open = False
                self.draw()
            return True
        if isinstance(key, int) and ord("1") <= key <= ord("4") and self.state.panel_open:
            self._open_panel(key - ord("1"))
            return True
        if key in (10, 13):  # Enter
            self._submit()
            return True
        if key == curses.KEY_UP:
            self._history(-1)
            return True
        if key == curses.KEY_DOWN:
            self._history(1)
            return True
        if key in (curses.KEY_PPAGE,):
            state.scroll = min(state.scroll + 10, scroll_limit(len(state.rows), self._body_height()))
            return True
        if key in (curses.KEY_NPAGE,):
            state.scroll = max(0, state.scroll - 10)
            return True
        if key == curses.KEY_HOME:
            state.scroll = scroll_limit(len(state.rows), self._body_height())
            return True
        if key == curses.KEY_END:
            state.scroll = 0
            return True
        if key in (curses.KEY_BACKSPACE, 127, 8):
            state.buffer = state.buffer[:-1]
            return True
        if key == 21:  # ^U clears the line
            state.buffer = ""
            return True
        if 32 <= key < 127 or key > 127:
            try:
                state.buffer += chr(key)
            except (ValueError, OverflowError):
                pass
            return True
        return True

    # ----------------------------------------------------------------- panels
    def _cycle_panel(self) -> None:
        """Tab: open the next panel, or close when they have all been seen."""
        if not self.state.panel_open:
            self.state.panel_open = True
            self.state.panel_index = 0
            self._prepare_panel()
            self.draw()
            return
        self.state.panel_index += 1
        if self.state.panel_index >= len(panels.PANELS):
            self.state.panel_open = False
            self.state.panel_index = 0
        else:
            self._prepare_panel()
        self.draw()

    def _open_panel(self, index: int) -> None:
        self.state.panel_open = True
        self.state.panel_index = max(0, min(index, len(panels.PANELS) - 1))
        self._prepare_panel()
        self.draw()

    def _prepare_panel(self) -> None:
        """Load whatever the panel needs *before* the next repaint.

        The graph scan takes about a second on this project, so it runs in a
        worker thread and the panel says "считаю…" until it lands — freezing
        the interface for a second to draw a side panel would be worse than
        waiting for it.
        """
        name = panels.PANELS[self.state.panel_index][0]
        if name == "plan":
            self._reload_plan()
        elif name == "graph" and not self.state.graph_rows and not self.state.graph_loading:
            self.state.graph_loading = True
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self._build_graph_sync()
                return
            loop.create_task(asyncio.to_thread(self._build_graph_sync))

    def _reload_plan(self) -> None:
        """The todo list the agent keeps in `.xli/todos.json`."""
        import json

        from xli.paths import xli_path

        items: list = []
        try:
            path = xli_path("todos.json")
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                items = [
                    panels.PlanItem(text=str(entry.get("text", "")), done=bool(entry.get("done")))
                    for entry in raw
                    if isinstance(entry, dict)
                ]
        except Exception:  # noqa: BLE001 - a broken todo file must not kill the UI
            items = []
        if items:
            current = next((item for item in items if not item.done), None)
            if current is not None:
                current.current = True
        self.state.plan = items

    def _build_graph_sync(self) -> None:
        """Build the dependency panel (blocking; always called off the UI path)."""
        try:
            from pathlib import Path

            from xli.ui import graph as graph_ui

            model = graph_ui.build(Path.cwd(), prefix="xli" if (Path.cwd() / "xli").is_dir() else "")
            rows = graph_ui.panel_rows(model, width=max(30, self._width() - 8), limit=6)
            from xli.tui.frame import fit

            self.state.graph_rows = [fit(row, max(20, self._width() - 4)) for row in rows]
        except Exception as exc:  # noqa: BLE001 - the panel is optional
            self.state.graph_rows = [[("граф недоступен: " + str(exc)[:60], "bad")]]
        finally:
            self.state.graph_loading = False
        self.draw()

    def _body_height(self) -> int:
        height, _ = self.screen.getmaxyx()
        return make_layout(0, height).body_height

    def _history(self, direction: int) -> None:
        state = self.state
        if not state.history:
            return
        if direction < 0:
            if state.history_index == -1:
                state.history_index = len(state.history) - 1
            elif state.history_index > 0:
                state.history_index -= 1
        else:
            if state.history_index == -1:
                return
            state.history_index += 1
            if state.history_index >= len(state.history):
                state.history_index = -1
                state.buffer = ""
                return
        state.buffer = state.history[state.history_index]

    def _submit(self) -> None:
        text = self.state.buffer.strip()
        self.state.buffer = ""
        self.state.history_index = -1
        if not text:
            return
        self.state.history.append(text)

        if text.startswith("/"):
            self._slash(text)
            return

        for row in transcript_row("user", {"text": text}, self._width()):
            self.state.rows.append(row)
        self._run_task(text)

    def _slash(self, text: str) -> None:
        parts = text[1:].split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""
        width = self._width()

        def note(message: str) -> None:
            self.state.rows.extend(transcript_row("note", {"text": message}, width))

        if command == "help":
            self.state.show_help = not self.state.show_help
        elif command == "tools":
            names = self.registry.names() if self.registry else []
            note(t("tui_tools_list", names=", ".join(names)))
        elif command == "mode" and argument in ("auto", "confirm", "readonly"):
            self.state.mode = argument
            self.config.set("permissions.mode", argument)
            if self.policy is not None:
                from xli.permissions.policy import Mode

                self.policy.mode = Mode.parse(argument)
            note(t("tui_mode", mode=argument))
        elif command == "model" and argument:
            self.state.model = argument
            self.config.set("provider.model", argument)
            note(t("tui_model", model=argument))
        elif command == "session":
            note(t("tui_session", session=self.state.session_id or "(none)"))
        elif command in ("panel", "panels"):
            names = {key: index for index, (key, _) in enumerate(panels.PANELS)}
            key = argument.strip().lower()
            if key in names:
                self._open_panel(names[key])
                return
            if key.isdigit() and 1 <= int(key) <= len(panels.PANELS):
                self._open_panel(int(key) - 1)
                return
            self._cycle_panel()
            return
        elif command == "plan":
            self._open_panel(0)
            return
        elif command == "graph":
            self._open_panel(1)
            return
        elif command == "agents":
            self._open_panel(2)
            return
        elif command in ("stats", "stat"):
            self._open_panel(3)
            return
        elif command == "clear":
            self.state.rows = []
            self.state.scroll = 0
        elif command in ("quit", "exit"):
            raise SystemExit(0)
        else:
            note(t("tui_unknown_cmd", command=command))

        self.draw()

    def _handle_approval_key(self, key: int) -> bool:
        if key in (ord("y"), ord("Y")):
            self._resolve_approval(True)
        elif key in (ord("n"), ord("N"), 27):
            self._resolve_approval(False)
        elif key in (ord("a"), ord("A")):
            self.state.allow_all = True
            self._resolve_approval(True)
        elif key in (3, 4):
            self._resolve_approval(False)
            return False
        return True

    def _resolve_approval(self, approved: bool) -> None:
        future = self.state.approval_future
        self.state.approval_future = None
        tool = (self.state.approval or {}).get("tool", "")
        self.state.approval = None
        self.state.rows.extend(
            transcript_row(
                "note",
                {"text": f"{tool}: {t('tui_approved') if approved else t('tui_refused')}"},
                self._width(),
            )
        )
        if future is not None and not future.done():
            future.set_result(approved)
        self.draw()

    def _width(self) -> int:
        """Usable columns: the terminal width minus the last cell.

        Curses cannot write the final cell of a line, so a row built at the
        exact terminal width loses its last character — which is the frame's
        right border on every single line.
        """
        if self.screen is None:
            return 80
        _, width = self.screen.getmaxyx()
        return max(20, width - 1)

    # ------------------------------------------------------------------ agent
    def append_event(self, kind: str, payload: dict[str, Any]) -> None:
        """Add one event to the transcript and to the panels' state.

        The panels are fed here rather than recomputed on repaint: a repaint
        happens every 50 ms, and rebuilding the counters from the transcript
        that often would burn the frame budget for no new information.
        """
        payload = dict(payload)
        name = str(payload.get("agent_name") or "")
        if name and payload.get("agent_id", "main") != "main":
            payload.setdefault("agent_hint", self._hint_for(name))

        width = self._width()
        self.state.rows.extend(transcript_row(kind, payload, width))

        if kind == "tool_call":
            self.state.counters["tools"] = self.state.counters.get("tools", 0) + 1
            tool = str(payload.get("name", ""))
            self.state.tool_counts[tool] = self.state.tool_counts.get(tool, 0) + 1
            if tool == "delegate":
                target = str((payload.get("args") or {}).get("agent_name", "?"))
                self._remember_delegate(target, (payload.get("args") or {}).get("task", ""), start=True)
                self.state.activity = t("tui_activity_delegating", agent=target)
            elif tool == "think":
                self.state.activity = t("tui_activity_thinking")
            else:
                self.state.activity = t("tui_activity_tool", tool=tool)
        elif kind == "tool_result":
            if not payload.get("ok"):
                self.state.counters["errors"] = self.state.counters.get("errors", 0) + 1
        elif kind == "step":
            index = int(payload.get("index", 0) or 0)
            self.state.counters["steps"] = index
            seconds = payload.get("seconds")
            if isinstance(seconds, (int, float)) and seconds > 0:
                self.state.step_seconds.append(float(seconds))
            self.state.activity = t("tui_activity_thinking")
        elif kind == "assistant":
            self.state.activity = t("tui_activity_writing")
        elif kind == "agent":
            agent = str(payload.get("agent_name") or payload.get("name") or "?")
            if str(payload.get("phase")) == "end":
                self._remember_delegate(
                    agent,
                    payload.get("task", ""),
                    start=False,
                    steps=payload.get("steps", 0),
                )
                self.state.activity = ""
            else:
                self._remember_delegate(agent, payload.get("task", ""), start=True)
        elif kind in ("error", "warning"):
            self.state.activity = ""

        if self.state.scroll == 0:
            self.draw()

    def _hint_for(self, name: str) -> str:
        try:
            from xli.ui.agents import agent_hint_for

            return agent_hint_for(name, str(self.config.project_root) if hasattr(self.config, "project_root") else None)
        except Exception:  # noqa: BLE001 - identity is cosmetic
            return ""

    def _remember_delegate(self, name: str, task: str = "", *, start: bool, steps: int = 0) -> None:
        from xli.ui.agents import agent_color, agent_hint_for

        entry = self.state.delegates.get(name) or {}
        if start:
            entry.setdefault("started", time.monotonic())
            entry["colour"] = agent_color(name, agent_hint_for(name))
            entry["status"] = "работает"
        else:
            entry["finished"] = time.monotonic()
            entry["status"] = "готово"
        if task:
            entry["task"] = task
        if steps:
            entry["steps"] = steps
        self.state.delegates[name] = entry

    def request_approval(self, tool: str, args: dict[str, Any], reason: str) -> asyncio.Future:
        # Called from inside the app's running loop, so get_running_loop().
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.state.approval = {"tool": tool, "args": args, "reason": reason}
        self.state.approval_future = future
        self.draw()
        return future

    def _debug_log(self, message: str) -> None:
        if self._debug:
            with open(self._debug, "a", encoding="utf-8") as handle:
                handle.write(message + "\n")

    def _run_task(self, task: str) -> None:
        if self._agent_task is not None and not self._agent_task.done():
            self.append_event("warning", {"message": t("tui_already")})
            return

        async def runner() -> None:
            from xli.agent import Agent
            from xli.session import Session
            from xli.tools.registry import default_registry

            registry = self.registry or default_registry(policy=self.policy)
            session = self.session or Session()
            self.state.session_id = session.session_id
            self.state.busy = True
            self.draw()

            if self.policy is not None and not self.state.allow_all:
                registry.confirm_handler = self._confirm_async

            self._debug_log(
                f"runner start: provider={type(self.provider).__name__} registry={bool(registry)} policy={self.policy!r}"
            )
            agent = Agent(
                self.provider,
                registry=registry,
                policy=self.policy,
                session=session,
                max_steps=int(self.config.get("agent.max_steps")),
                on_event=self.append_event,
            )
            try:
                result = await agent.run(task)
                reason = t(f"stop_{result.stopped_reason}") if result.stopped_reason else ""
                self.append_event(
                    "note",
                    {"text": f"{result.summary}" + (f"  ·  {reason}" if reason else "")},
                )
            except Exception as exc:  # noqa: BLE001 - keep the UI alive
                import traceback

                self._debug_log("runner failed: " + traceback.format_exc())
                self.append_event("error", {"message": f"{type(exc).__name__}: {exc}"})
            finally:
                self.state.busy = False
                self.draw()

        self._agent_task = asyncio.create_task(runner())

    async def _confirm_async(self, tool: str, args: dict[str, Any], reason: str) -> bool:
        """Bridge the policy's confirmation callback onto the async approval UI.

        This used to be a synchronous method that pumped the loop with
        loop.run_until_complete(asyncio.sleep(...)). It is called from the
        registry, which runs inside this same event loop, so that call raised
        "This event loop is already running" and every confirmation in the TUI
        failed. The registry now accepts a coroutine handler, so this awaits
        properly instead.
        """
        if self.state.allow_all:
            return True
        future = self.request_approval(tool, args, reason)
        while not future.done():
            self._pump_input()
            await asyncio.sleep(0.02)
        return bool(future.result())

    def _pump_input(self) -> None:
        self.screen.nodelay(True)
        try:
            while True:
                key = self._read_char()
                if key == -1:
                    break
                if not self._handle_key(key):
                    raise SystemExit(0)
        finally:
            self.screen.nodelay(False)

    # -------------------------------------------------------------------- run
    async def run_async(self, initial_task: str = "") -> int:
        self.screen.nodelay(False)
        self.screen.timeout(50)  # wake regularly so agent events repaint promptly

        if initial_task:
            self.state.buffer = initial_task
            self._submit()

        while True:
            if self._interrupted:
                return 130
            key = self._read_char()
            if key == -1:
                self.draw()
                # Hand the event loop back. `run_async` is a coroutine, but a
                # coroutine that never awaits never yields — and the agent's
                # task, created by `_run_task`, needs the loop to run it. The
                # interface painted beautifully and answered nothing, because
                # the work it was waiting for was queued behind a loop that
                # never gave it a turn.
                await asyncio.sleep(0.01)
                continue
            if self._debug:
                with open(self._debug, "a", encoding="utf-8") as handle:
                    handle.write(f"key={key!r}\n")
            if not self._handle_key(key):
                break
            self.draw()
            await asyncio.sleep(0)
        return 0


def _curses_main(stdscr, app: Tui, initial_task: str) -> int:
    app.screen = stdscr
    app.attrs = _init_colors()
    curses.curs_set(0)
    stdscr.keypad(True)
    app._seed_history()
    app.draw()
    return asyncio.run(app.run_async(initial_task))


def _setup_locale() -> str:
    """Ask curses for UTF-8, returning the encoding it settled on.

    Without this the process runs in the C locale, curses reads the keyboard a
    byte at a time, and every multibyte character arrives as several
    characters. Typing "привет" put "Ð¿ÑÐ¸Ð²ÐµÑ" in the buffer, and wide
    glyphs were measured one cell short so the frame walked out of alignment.

    The user's locale is tried first; if it is unset or unsupported, a UTF-8
    locale is forced, because a terminal that can show the text is worth more
    than a locale name that matches the environment.
    """
    import locale

    for candidate in ("", "C.UTF-8", "en_US.UTF-8", "ru_RU.UTF-8", "UTF-8"):
        try:
            locale.setlocale(locale.LC_ALL, candidate)
        except locale.Error:
            continue
        encoding = (locale.getencoding() or "").lower()
        if encoding in ("utf-8", "utf8"):
            return "utf-8"

    try:
        locale.setlocale(locale.LC_CTYPE, "")
    except locale.Error:
        pass
    return (locale.getencoding() or "ascii").lower()


def run_tui(config, *, initial_task: str = "", registry=None, policy=None, provider=None) -> int:
    """Entry point used by `xli tui`. Returns a process exit code."""
    if not sys.stdout.isatty():
        print("the TUI needs a terminal — use `xli run` or `xli repl` instead", file=sys.stderr)
        return 2

    # Before curses starts: the locale decides whether the keyboard delivers
    # characters or bytes.
    _setup_locale()

    app = Tui(config, registry=registry, policy=policy, provider=provider)

    # ^C in a pty is a signal to the foreground process group, not a keypress.
    # Handling it here — rather than letting it interrupt a coroutine — keeps
    # the exit deterministic and gets the terminal restored by curses.wrapper.
    import signal

    previous_handler = signal.getsignal(signal.SIGINT)

    def _on_interrupt(signum, frame):  # noqa: ARG001 - signal signature
        app._interrupted = True

    try:
        signal.signal(signal.SIGINT, _on_interrupt)
    except ValueError:  # not the main thread: nothing to install
        pass

    try:
        return curses.wrapper(_curses_main, app, initial_task)
    except KeyboardInterrupt:
        return 130
    except SystemExit as exc:
        return int(exc.code or 0)
    except BaseException:  # noqa: BLE001 - diagnose, then re-raise
        if app._debug:
            import traceback

            with open(app._debug, "a", encoding="utf-8") as handle:
                handle.write("crashed: " + traceback.format_exc())
        raise
    finally:
        try:
            signal.signal(signal.SIGINT, previous_handler)
        except (ValueError, TypeError):
            pass
