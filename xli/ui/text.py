#!/usr/bin/env python3
"""Display-width helpers for terminal text.

`len(text)` counts characters. A terminal counts *cells*, and the two disagree:

    len("привет")  == 6   and it occupies 6 cells   — fine
    len("日本語")   == 3   and it occupies 6 cells   — off by 3
    len("🎉")      == 1   and it occupies 2 cells   — off by 1

The TUI used len() everywhere it laid out a line, so anything wider than one
cell pushed the frame out of alignment, and the cursor drifted off the text it
was supposed to sit on. Cyrillic itself is one cell per character, but the same
code paths handle emoji, box-drawing and CJK, so the measurement has to be
right for all of them or it is wrong for the ones that matter.

There is no dependency here: the width tables come from Unicode's East Asian
Width property, narrowed to the ranges that actually occur.
"""

from __future__ import annotations

import unicodedata
from typing import Any

#: Ranges whose characters occupy two terminal cells.
#: East Asian Wide (W) and Fullwidth (F), plus emoji presentation.
_WIDE_RANGES: tuple[tuple[int, int], ...] = (
    (0x1100, 0x115F),    # Hangul Jamo init. consonants
    (0x2E80, 0x303E),    # CJK radicals, Kangxi, CJK symbols
    (0x3041, 0x33FF),    # Hiragana .. CJK compatibility
    (0x3400, 0x4DBF),    # CJK ext A
    (0x4E00, 0x9FFF),    # CJK unified
    (0xA000, 0xA4CF),    # Yi
    (0xA960, 0xA97F),    # Hangul Jamo ext A
    (0xAC00, 0xD7A3),    # Hangul syllables
    (0xF900, 0xFAFF),    # CJK compatibility ideographs
    (0xFE10, 0xFE19),    # vertical forms
    (0xFE30, 0xFE6F),    # CJK compatibility forms
    (0xFF00, 0xFF60),    # fullwidth forms
    (0xFFE0, 0xFFE6),    # fullwidth signs
    (0x16FE0, 0x16FE4),
    (0x17000, 0x18AFF),
    (0x1B000, 0x1B12F),
    (0x1F004, 0x1F004),
    (0x1F0CF, 0x1F0CF),
    (0x1F18E, 0x1F18E),
    (0x1F191, 0x1F19A),
    (0x1F200, 0x1F320),
    (0x1F32D, 0x1F335),
    (0x1F337, 0x1F37C),
    (0x1F37E, 0x1F393),
    (0x1F3A0, 0x1F3CA),
    (0x1F3CF, 0x1F3D3),
    (0x1F3E0, 0x1F3F0),
    (0x1F3F4, 0x1F3F4),
    (0x1F3F8, 0x1F43E),
    (0x1F440, 0x1F440),
    (0x1F442, 0x1F4FC),
    (0x1F4FF, 0x1F53D),
    (0x1F54B, 0x1F54E),
    (0x1F550, 0x1F567),
    (0x1F57A, 0x1F57A),
    (0x1F595, 0x1F596),
    (0x1F5A4, 0x1F5A4),
    (0x1F5FB, 0x1F64F),
    (0x1F680, 0x1F6C5),
    (0x1F6CC, 0x1F6CC),
    (0x1F6D0, 0x1F6D2),
    (0x1F6EB, 0x1F6EC),
    (0x1F6F4, 0x1F6FC),
    (0x1F7E0, 0x1F7EB),
    (0x1F90C, 0x1F93A),
    (0x1F93C, 0x1F945),
    (0x1F947, 0x1F978),
    (0x1F97A, 0x1F9CB),
    (0x1F9CD, 0x1F9FF),
    (0x1FA70, 0x1FAFF),
    (0x20000, 0x3FFFD),  # CJK ext B and beyond
)

#: Zero-width: combining marks, format controls, variation selectors.
_ZERO_WIDTH_RANGES: tuple[tuple[int, int], ...] = (
    (0x0300, 0x036F),    # combining diacriticals
    (0x1160, 0x11FF),    # Hangul Jamo medial vowels / final consonants
    (0x0483, 0x0489),
    (0x0591, 0x05BD),
    (0x200B, 0x200F),    # zero-width space .. RLM
    (0x2028, 0x202E),
    (0x2060, 0x2064),
    (0xFE00, 0xFE0F),    # variation selectors
    (0xFEFF, 0xFEFF),
    (0xE0100, 0xE01EF),
)


def _in_ranges(code: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(low <= code <= high for low, high in ranges)


def char_width(char: str) -> int:
    """Terminal cells occupied by one character."""
    if not char:
        return 0
    code = ord(char)
    if code < 0x20 or code == 0x7F:
        return 0                      # control characters render as nothing
    if _in_ranges(code, _ZERO_WIDTH_RANGES):
        return 0
    if unicodedata.combining(char):
        return 0
    if _in_ranges(code, _WIDE_RANGES):
        return 2
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return 2
    return 1


def display_width(text: str) -> int:
    """Total terminal cells a string occupies."""
    return sum(char_width(c) for c in text)


def truncate(text: str, width: int, *, ellipsis: str = "…") -> str:
    """Cut `text` to at most `width` cells, appending an ellipsis if cut.

    Cuts on a cell boundary, never through a wide character or a combining
    mark, which is what slicing by index does.
    """
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text

    budget = width - display_width(ellipsis)
    if budget <= 0:
        # No room for content *and* the ellipsis. Signalling that something was
        # cut matters more than showing one stray character, so prefer the
        # ellipsis when it fits at all.
        if display_width(ellipsis) <= width:
            return ellipsis
        out, used = "", 0
        for char in text:
            step = char_width(char)
            if used + step > width:
                break
            out += char
            used += step
        return out

    out, used = "", 0
    for char in text:
        step = char_width(char)
        if used + step > budget:
            break
        out += char
        used += step
    return out + ellipsis


def pad(text: str, width: int, align: str = "left") -> str:
    """Pad to exactly `width` cells, truncating first if the text is wider.

    Callers lay rows into a fixed frame, so returning something longer than
    `width` is never acceptable — an over-long row pushes the frame out of
    alignment on every redraw.
    """
    if width <= 0:
        return ""
    text = truncate(text, width)
    gap = width - display_width(text)
    if gap <= 0:
        return text
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def split_cells(text: str, width: int) -> list[str]:
    """Split into chunks of at most `width` cells, without breaking a glyph."""
    if width <= 0:
        # Nothing fits, so return nothing. Claiming a 5-cell chunk fits in a
        # 0-cell budget is the kind of quiet lie that overflows a frame later.
        return []
    chunks: list[str] = []
    current, used = "", 0
    for char in text:
        step = char_width(char)
        if step == 0:                     # combining mark rides with its base
            current += char
            continue
        if used + step > width and current:
            chunks.append(current)
            current, used = "", 0
        current += char
        used += step
    if current:
        chunks.append(current)
    return chunks or [""]


def json_dumps(value: Any, limit: int = 300) -> str:
    """Serialise for logs. Truncation keeps an ASCII ``"..."`` suffix.

    This is a serialisation helper, not a display helper, so it deliberately
    avoids the typographic ellipsis: log lines and nvim payloads are consumed
    by things that should not have to assume UTF-8.
    """
    import json

    try:
        text = json.dumps(value, ensure_ascii=False, indent=None, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(value)
    if display_width(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."
