#!/usr/bin/env python3
"""Markdown rendering for terminals.

Two front ends need this and neither should own it: the TUI paints styled spans
into curses, and the CLI prints ANSI to stdout. So this module produces neither
-- it produces the same (text, style) spans the TUI already uses, and a separate
function in `xli/cli.py` turns those into ANSI. One parser, two painters.

It is deliberately a small hand-written parser rather than a dependency. The
alternative is `rich`, which is an optional extra under `[tui]`; making the CLI
depend on it would mean `xli run` stopped working on a bare install, which is
the exact failure `_http.py` was written to fix.

Supported: ATX and setext headings, fenced code with a language tag, bullet /
numbered / task lists, block quotes, horizontal rules, tables, and inline bold,
italic, strikethrough, code, links and autolinks. Emphasis nests. Anything
unrecognised passes through as plain text, so a malformed document degrades to
readable prose instead of raising -- the agent is cut off mid-code fence often
enough that this is a normal case, not an edge case.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from xli.ui.syntax import highlight_line

Span = tuple[str, str]
Row = list[Span]

# Style names. These are the TUI's vocabulary; the ANSI painter maps them too.
NORMAL = "normal"
DIM = "dim"
BOLD = "bold"
ACCENT = "accent"
GOOD = "good"
WARN = "warn"
BAD = "bad"
CODE = "code"
QUOTE = "quote"
HEADING = "heading"
HEADING2 = "heading2"
HEADING3 = "heading3"
LINK = "link"
ITALIC = "italic"
STRIKE = "strike"
#: `==выделение==`. GFM's extension, and the one models reach for when they
#: want to point at the important word in a paragraph.
MARK = "mark"
#: Horizontal rules get a style of their own so the painters can render them
#: as a gradient instead of a grey dash line.
RULE = "rule"
#: Syntax highlighting inside fenced code. ``code`` is the default text;
#: these carry the meaning. Painters map them like any other style.
KW = "kw"       # language keyword
STR = "str"     # string literal
NUM = "num"     # number literal
COM = "com"     # comment
FN = "fn"       # function / method name
OP = "op"       # punctuation and operators


@dataclass
class Block:
    """One markdown block, already split from its neighbours."""

    kind: str                       # paragraph, heading, code, list, quote, rule, table
    text: str = ""
    level: int = 0                  # heading level, or list indent
    language: str = ""              # code fence language
    items: list[dict] = field(default_factory=list)
    ordered: bool = False
    rows: list[list[str]] = field(default_factory=list)
    header: bool = False            # table: first row is a header
    inline: list[Span] = field(default_factory=list)

    # Convenience aliases, so callers do not have to know the storage layout.
    @property
    def lang(self) -> str:
        return self.language

    @property
    def lines(self) -> list[str]:
        return self.text.split("\n") if self.text else []


_FENCE = re.compile(r"^(\s*)(```+|~~~+)\s*([A-Za-z0-9_+#.-]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_SETEXT_H1 = re.compile(r"^=+\s*$")
_SETEXT_H2 = re.compile(r"^-+\s*$")
_BULLET = re.compile(r"^(\s*)([-*+])\s+(.*)$")
_ORDERED = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_TASK = re.compile(r"^\[([ xX])\]\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_RULE = re.compile(r"^\s{0,3}([-*_])\s*(?:\1\s*){2,}$")
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
# GFM allows a single dash per column, so `| - | - |` is a valid separator.
# One column is legal too: `| --- |` separates a single-column table.
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
_AUTOLINK = re.compile(r"https?://[^\s<>)\]]+", re.IGNORECASE)


def parse(markdown: str) -> list[Block]:
    """Split markdown into blocks. Never raises on malformed input."""
    lines = (markdown or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[Block] = []
    index = 0

    while index < len(lines):
        line = lines[index]

        # ---- fenced code ------------------------------------------------
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(2)
            language = fence.group(3)
            body: list[str] = []
            index += 1
            while index < len(lines):
                closing = _FENCE.match(lines[index])
                # An unterminated fence runs to the end of the document; the
                # agent is cut off mid-block often enough that losing the tail
                # would hide exactly the code the user is asking about.
                if closing and closing.group(2)[0] == marker[0]:
                    index += 1
                    break
                body.append(lines[index])
                index += 1
            blocks.append(Block(kind="code", text="\n".join(body), language=language))
            continue

        # ---- ATX heading -------------------------------------------------
        heading = _HEADING.match(line)
        if heading:
            text = heading.group(2).strip()
            blocks.append(
                Block(
                    kind="heading",
                    text=text,
                    level=len(heading.group(1)),
                    inline=inline_spans(text),
                )
            )
            index += 1
            continue

        # ---- horizontal rule ---------------------------------------------
        # Checked before setext, because `---` alone is a rule but `---` under
        # a line of text is a level-2 heading.
        if _RULE.match(line) and not (index and lines[index - 1].strip()):
            blocks.append(Block(kind="rule"))
            index += 1
            continue

        # ---- setext heading -----------------------------------------------
        if (
            line.strip()
            and index + 1 < len(lines)
            and (_SETEXT_H1.match(lines[index + 1]) or _SETEXT_H2.match(lines[index + 1]))
            and not _FENCE.match(line)
            and not _QUOTE.match(line)
            and not _BULLET.match(line)
            and not _ORDERED.match(line)
        ):
            level = 1 if _SETEXT_H1.match(lines[index + 1]) else 2
            text = line.strip()
            blocks.append(
                Block(kind="heading", text=text, level=level, inline=inline_spans(text))
            )
            index += 2
            continue

        # ---- table ---------------------------------------------------------
        if (
            _TABLE_ROW.match(line)
            and index + 1 < len(lines)
            and _TABLE_SEP.match(lines[index + 1])
        ):
            rows = [_split_row(line)]
            index += 2
            while index < len(lines) and _TABLE_ROW.match(lines[index]):
                rows.append(_split_row(lines[index]))
                index += 1
            # Ragged rows are common in generated tables; even them out so the
            # renderer does not have to guess.
            columns = max(len(row) for row in rows)
            rows = [row + [""] * (columns - len(row)) for row in rows]
            blocks.append(Block(kind="table", rows=rows, header=True))
            continue

        # ---- block quote ----------------------------------------------------
        quote = _QUOTE.match(line)
        if quote:
            body = [quote.group(1)]
            index += 1
            while index < len(lines):
                more = _QUOTE.match(lines[index])
                if not more:
                    break
                body.append(more.group(1))
                index += 1
            text = "\n".join(body)
            blocks.append(Block(kind="quote", text=text, inline=inline_spans(text)))
            continue

        # ---- list -------------------------------------------------------------
        bullet = _BULLET.match(line)
        ordered = _ORDERED.match(line)
        if bullet or ordered:
            match = bullet or ordered
            is_ordered = bool(ordered)
            indent = len(match.group(1))
            items = [_make_item(match, is_ordered)]
            index += 1
            while index < len(lines):
                more = _BULLET.match(lines[index]) or _ORDERED.match(lines[index])
                if not more:
                    # A continuation line belongs to the previous item.
                    if lines[index].strip() and lines[index].startswith(" " * (indent + 1)):
                        items[-1]["text"] += " " + lines[index].strip()
                        index += 1
                        continue
                    break
                if bool(_ORDERED.match(lines[index])) != is_ordered:
                    break
                items.append(_make_item(more, is_ordered))
                index += 1
            blocks.append(Block(kind="list", items=items, ordered=is_ordered, level=indent))
            continue

        # ---- blank -------------------------------------------------------------
        if not line.strip():
            index += 1
            continue

        # ---- paragraph -----------------------------------------------------------
        body = [line]
        index += 1
        while index < len(lines):
            nxt = lines[index]
            if (
                not nxt.strip()
                or _FENCE.match(nxt)
                or _HEADING.match(nxt)
                or _SETEXT_H1.match(nxt)
                or _SETEXT_H2.match(nxt)
                or _RULE.match(nxt)
                or _BULLET.match(nxt)
                or _ORDERED.match(nxt)
                or _QUOTE.match(nxt)
                or _TABLE_ROW.match(nxt)
            ):
                break
            body.append(nxt)
            index += 1
        text = " ".join(s.strip() for s in body)
        blocks.append(Block(kind="paragraph", text=text, inline=inline_spans(text)))

    return blocks


def _make_item(match: re.Match, is_ordered: bool) -> dict:
    """One list item, with its marker state pulled apart."""
    raw = match.group(3)
    # The item's own indent, not the block's: a nested list arrives in the
    # same block (the marker regex matches every level), and without this the
    # renderer cannot tell a sub-item from its parent.
    indent = len(match.group(1))
    task = _TASK.match(raw)
    if task:
        return {
            "text": task.group(2),
            "number": int(match.group(2)) if is_ordered else 0,
            "checked": task.group(1).lower() == "x",
            "task": True,
            "indent": indent,
        }
    return {
        "text": raw,
        "number": int(match.group(2)) if is_ordered else 0,
        "checked": False,
        "task": False,
        "indent": indent,
    }


def _split_row(line: str) -> list[str]:
    inner = line.strip().strip("|")
    return [cell.strip() for cell in inner.split("|")]


# ---------------------------------------------------------------- inline spans
# Marker length matters: longer markers are tried first so `***x***` is not
# read as `*` wrapping `**x**`.
_EMPHASIS = (
    ("***", BOLD),
    ("___", BOLD),
    ("**", BOLD),
    ("__", BOLD),
    ("~~", STRIKE),
    ("==", MARK),
    ("*", ITALIC),
    ("_", ITALIC),
)

_MAX_DEPTH = 6


def inline_spans(text: str, base: str = NORMAL, *, _depth: int = 0) -> list[Span]:
    """Turn inline markdown into styled spans, leaving prose untouched.

    Recursive, so emphasis nests: `*a **b** c*` yields italic around bold.
    Unmatched markers stay literal, because `2 * 3 * 4` is arithmetic more
    often than it is emphasis.
    """
    if not text:
        return []
    if _depth > _MAX_DEPTH:
        return [(text, base)]

    out: list[Span] = []
    buffer: list[str] = []
    index = 0
    length = len(text)

    def flush() -> None:
        if buffer:
            out.append(("".join(buffer), base))
            buffer.clear()

    while index < length:
        char = text[index]

        # ---- code span: highest priority, nothing nests inside it
        if char == "`":
            match = re.match(r"(`+)(.+?)\1", text[index:], re.DOTALL)
            if match:
                flush()
                out.append((match.group(2), CODE))
                index += match.end()
                continue

        # ---- explicit link [label](url)
        if char == "[":
            match = re.match(r"\[([^\]]*)\]\(([^)\s]*)\)", text[index:])
            if match:
                flush()
                label = match.group(1) or match.group(2)
                # The label only: `[текст](url)` is how a model cites a source,
                # and printing the address doubles the width of every citation
                # for information the reader rarely needs at that moment.
                out.append((label, LINK))
                index += match.end()
                continue

        # ---- autolink <https://...>
        if char == "<":
            match = re.match(r"<((?:https?|ftp)://[^>\s]+)>", text[index:])
            if match:
                flush()
                out.append((match.group(1), LINK))
                index += match.end()
                continue

        # ---- bare autolink
        if char in "hH" and text[index : index + 4].lower() == "http":
            previous = text[index - 1] if index else ""
            if not (previous.isalnum() or previous in "/_-"):
                match = _AUTOLINK.match(text, index)
                if match:
                    flush()
                    out.append((match.group(0).rstrip(".,;:"), LINK))
                    index += len(match.group(0).rstrip(".,;:"))
                    continue

        # ---- emphasis
        matched = False
        for marker, style in _EMPHASIS:
            if not text.startswith(marker, index):
                continue
            after = index + len(marker)
            # An opener cannot be followed by whitespace.
            if after >= length or text[after].isspace():
                continue
            # `_` does not open emphasis inside a word, so snake_case survives.
            if marker[0] == "_" and index and (text[index - 1].isalnum()):
                continue
            close = _find_close(text, after, marker)
            if close < 0:
                continue
            inner = text[after:close]
            if not inner.strip():
                continue
            flush()
            out.extend(inline_spans(inner, style, _depth=_depth + 1))
            index = close + len(marker)
            matched = True
            break
        if matched:
            continue

        buffer.append(char)
        index += 1

    flush()
    return [span for span in out if span[0]]


def _find_close(text: str, start: int, marker: str) -> int:
    """Index of the delimiter that closes `marker`, or -1.

    The run length has to match exactly, so the `**` inside `*a **b** c*` is
    recognised as a nested delimiter rather than as the closing `*`. A closing
    delimiter also cannot be preceded by whitespace.
    """
    index = start
    length = len(text)
    size = len(marker)
    char = marker[0]

    while index < length:
        if text[index] == char:
            run = 0
            cursor = index
            while cursor < length and text[cursor] == char:
                run += 1
                cursor += 1
            if run == size and not (index and text[index - 1].isspace()):
                return index
            index = cursor
            continue
        index += 1
    return -1


def strip_inline(text: str) -> str:
    """Remove inline markup, keeping the words. For width-sensitive contexts."""
    return "".join(part for part, _ in inline_spans(text))


# ------------------------------------------------------------------- rendering
def render_rows(markdown: str, width: int, *, indent: int = 0) -> list[Row]:
    """Render markdown to rows of spans, wrapped to `width`.

    Returns the same row shape the TUI paints, so the two stay in step. Every
    row is guaranteed to fit inside `width` cells.
    """
    from xli.ui.text import display_width

    if width <= 0:
        return []

    rows: list[Row] = []
    gutter = " " * indent
    body_width = max(1, width - indent)

    def emit(row: Row) -> None:
        rows.append(_clamp(row, width))

    for block in parse(markdown):
        if block.kind == "heading":
            # Levels must be tellable apart at a glance: h1 is the violet bar,
            # h2 a softer purple, h3+ plain bold — a document whose headings
            # all wear one colour has no hierarchy on screen.
            if block.level == 1:
                prefix, style = "▌", HEADING
            elif block.level == 2:
                prefix, style = "▐", HEADING2
            else:
                prefix, style = "│", HEADING3
            spans: Row = [(f"{gutter}{prefix} ", ACCENT)]
            spans += [
                (t, style) for t, _ in (block.inline or inline_spans(block.text))
            ]
            for row in wrap_row(spans, width):
                emit(row)

        elif block.kind == "code":
            # The fence keeps its exact shape (`┌─ py ───`), because tests and
            # habits both read it; what changes is that the body is tokenised
            # when the language is known, so the code is scannable instead of
            # a flat block of lilac.
            label = f"─ {block.language} " if block.language else ""
            head = f"{gutter}┌{label}"
            # The bottom edge mirrors the top: a frame whose lid is wider than
            # its floor reads as a rendering bug even when the text is right.
            fill = max(3, body_width - display_width(head) - 1)
            emit([(head + "─" * fill, DIM)])
            for line in (block.lines or [""]):
                spans: Row = [(f"{gutter}│ ", DIM)]
                if block.language:
                    spans += highlight_line(line, block.language)
                else:
                    spans.append((line, CODE))
                emit(spans)
            emit([(f"{gutter}└" + "─" * fill, DIM)])

        elif block.kind == "list":
            base_indent = min(
                (int(item.get("indent", 0)) for item in block.items), default=0
            )
            for number, item in enumerate(block.items, start=1):
                # Nesting is drawn as indentation of the marker, which is how
                # a list reads in a terminal; the depth is the item's indent
                # relative to the shallowest item in the same block.
                depth = max(0, (int(item.get("indent", 0)) - base_indent) // 2)
                nest = "  " * depth
                marker_style = ACCENT
                if item.get("task"):
                    # A tick box you can read at a glance; the old "[x]" was
                    # three cells of punctuation the eye had to parse.
                    if item.get("checked"):
                        marker, marker_style = "☑", GOOD
                    else:
                        marker, marker_style = "☐", DIM
                elif block.ordered:
                    marker = f"{item.get('number') or number}."
                else:
                    marker = "•"
                lead = f"{gutter}  {nest}{marker} "
                marker_cell = f"{gutter}  {nest}{marker} "
                for row in wrap_row(
                    [(marker_cell, marker_style)] + inline_spans(item["text"]),
                    width,
                    hang=display_width(lead),
                    keep_leading=True,
                ):
                    emit(row)

        elif block.kind == "quote":
            for line in block.text.split("\n"):
                lead = f"{gutter}▎ "
                for row in wrap_row(
                    [(lead, DIM)] + inline_spans(line, QUOTE), width, hang=display_width(lead)
                ):
                    emit(row)

        elif block.kind == "rule":
            emit([(gutter + "─" * min(body_width, 40), RULE)])

        elif block.kind == "table":
            for row in _render_table(block.rows, width, gutter):
                emit(row)

        else:  # paragraph
            spans = ([(gutter, NORMAL)] if gutter else []) + (
                block.inline or inline_spans(block.text)
            )
            for row in wrap_row(spans, width, hang=indent):
                emit(row)

    return rows


def _clamp(row: Row, width: int) -> Row:
    """Last-resort guard: no row may exceed `width` cells."""
    from xli.ui.text import display_width, truncate

    total = display_width("".join(t for t, _ in row))
    if total <= width:
        return row
    overflow = total - width
    out: list[Span] = []
    for text, style in reversed(row):
        if overflow <= 0:
            out.append((text, style))
            continue
        keep = max(0, display_width(text) - overflow)
        overflow -= display_width(text) - keep
        if keep:
            out.append((truncate(text, keep), style))
    return list(reversed(out))


def _render_table(rows: list[list[str]], width: int, gutter: str) -> list[Row]:
    from xli.ui.text import display_width, pad, truncate

    if not rows:
        return []
    columns = max(len(row) for row in rows)
    rows = [row + [""] * (columns - len(row)) for row in rows]

    gutter_width = display_width(gutter)
    # Each column costs its content plus " │ " on both sides, plus one edge.
    overhead = columns * 3 + 1
    available = max(columns, width - gutter_width - overhead)

    natural = [
        max(display_width(strip_inline(row[i])) for row in rows) for i in range(columns)
    ]
    total = sum(natural) or 1
    if total > available:
        widths = [max(3, int(w * available / total)) for w in natural]
    else:
        widths = natural

    def border(left: str, mid: str, right: str) -> Row:
        return [(gutter + left + mid.join("─" * (w + 2) for w in widths) + right, DIM)]

    out: list[Row] = [border("┌", "┬", "┐")]
    for position, row in enumerate(rows):
        cells: Row = [(f"{gutter}│ ", DIM)]
        for index, cell in enumerate(row):
            text = truncate(strip_inline(cell), widths[index])
            cells.append(
                (pad(text, widths[index]), BOLD if position == 0 else NORMAL)
            )
            if position == 0:
                cells[-1] = (cells[-1][0], HEADING3)
            cells.append((" │ " if index < len(row) - 1 else " │", DIM))
        out.append(cells)
        if position == 0:
            out.append(border("├", "┼", "┤"))
    out.append(border("└", "┴", "┘"))
    return out


def wrap_row(
    row: Row, width: int, *, hang: int = 0, keep_leading: bool = False
) -> list[Row]:
    """Word-wrap a span row to `width` cells, preserving each span's style.

    Leading spaces are dropped by default, because a wrapped paragraph must
    not start with the space that ended the previous line. `keep_leading` asks
    for them anyway — the list renderer needs it, since its indentation *is*
    the nesting.
    """
    from xli.ui.text import char_width, display_width

    if width <= 0:
        return [[]]

    out: list[Row] = []
    current: Row = []
    used = 0
    continuation = " " * hang

    def flush() -> None:
        nonlocal current, used
        out.append(current)
        current, used = [], 0

    def start_continuation() -> None:
        nonlocal used
        if hang:
            current.append((continuation, NORMAL))
            used = hang

    for text, style in row:
        for line in text.split("\n"):
            if line != text.split("\n")[0]:
                flush()
                start_continuation()
            for word in _words(line):
                word_width = display_width(word)
                if word == " " and not current and not (keep_leading and not out):
                    continue
                if used + word_width > width and current:
                    flush()
                    start_continuation()
                    if word == " ":
                        continue
                # A single word wider than the line is split by cells, so a
                # long path or a CJK run still respects the frame.
                if word_width > width - (0 if current else hang):
                    for char in word:
                        step = char_width(char)
                        if step == 0:
                            current.append((char, style))
                            continue
                        if used + step > width and current:
                            flush()
                            start_continuation()
                        current.append((char, style))
                        used += step
                    continue
                current.append((word, style))
                used += word_width

    out.append(current)
    return out


def _words(text: str) -> list[str]:
    """Split on spaces, keeping them, so wrapping is lossless."""
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


def render_plain(markdown: str, width: int = 80) -> str:
    """Markdown as plain text, for logs and non-tty output."""
    from xli.ui.text import truncate

    lines: list[str] = []
    for row in render_rows(markdown, width):
        text = "".join(part for part, _ in row)
        lines.append(truncate(text, width))
    return "\n".join(lines)
