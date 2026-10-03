#!/usr/bin/env python3
"""XLI gradient engine — the one place colour is decided.

Every front end wants the same thing: this app's violet, in a gradient, that
lights up as it moves. "Lights up" was previously done with styles (dim →
accent → heading), which is a *three-step* brightness ladder — on a 256-colour
terminal a heading looked like a slightly different purple, not like a glow.

This module renders real colour: 24-bit when the terminal admits to it,
xterm-256 otherwise, and 16-colour/greyscale when that is all there is. It also
owns the small chart vocabulary (sparkline, bars, braille plot) so the CLI and
the TUI draw the same picture for the same numbers.

Rules this module follows:

* **Never emit colour when it is disabled.** ``enabled=False`` (or ``NO_COLOR``
  in the environment) returns the plain text. A piped log must stay readable.
* **Never measure a coloured string with ``len()``.** Escapes are not cells;
  :func:`visible_width` exists for the callers that need the width of
  something this module produced.
* **A gradient is a function of the text’s own width**, not of the terminal’s,
  so a short heading and a long rule look like the same material.
"""

from __future__ import annotations

import math
import os
import re
import sys
from dataclasses import dataclass

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
ITALIC = "\033[3m"
UNDERLINE = "\033[4m"

# ── XLI palette ─────────────────────────────────────────────────────────────
#: Deep violet — the dark end of every ramp.
XLI_DARK_VIOLET: tuple[int, int, int] = (90, 24, 154)     # #5A189A
#: The app's accent, XLI purple.
XLI_PURPLE: tuple[int, int, int] = (157, 78, 221)         # #9D4EDD
#: Neon magenta — the bright end.
XLI_NEON_MAGENTA: tuple[int, int, int] = (224, 170, 255)  # #E0AAFF
#: Near-white violet, for text that must out-shine a heading.
XLI_GLOW_WHITE: tuple[int, int, int] = (241, 226, 255)    # #F1E2FF
#: A calm green/violet pair used by charts, not by chrome.
XLI_MINT: tuple[int, int, int] = (126, 231, 176)          # #7EE7B0
XLI_AMBER: tuple[int, int, int] = (255, 196, 110)         # #FFC46E
XLI_RED: tuple[int, int, int] = (255, 122, 149)           # #FF7A95
XLI_BLUE: tuple[int, int, int] = (140, 190, 255)          # #8CBEFF

RGB = tuple[int, int, int]

_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")
_ANSI = re.compile(r"\033\[[0-9;]*m")


# --------------------------------------------------------------------- basics
def hex_to_rgb(value: str) -> RGB:
    """``"#9D4EDD"`` -> ``(157, 78, 221)``. Raises ValueError on nonsense."""
    match = _HEX.match(value.strip())
    if not match:
        raise ValueError(f"not a hex colour: {value!r}")
    digits = match.group(1)
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def rgb_to_hex(rgb: RGB) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, int(c))) for c in rgb))


def as_rgb(color) -> RGB | None:
    """Accept ``#RRGGBB`` or an RGB triple; return None for anything else.

    Front ends hand colours around as style names (hex strings) because that
    is what a span carries, and as tuples when they build a ramp by hand.
    Coercing here means neither caller needs a second helper.
    """
    if color is None:
        return None
    if isinstance(color, str):
        try:
            return hex_to_rgb(color)
        except ValueError:
            return None
    if isinstance(color, (tuple, list)) and len(color) == 3:
        try:
            return (int(color[0]), int(color[1]), int(color[2]))
        except (TypeError, ValueError):
            return None
    return None


def interpolate_color(start: RGB, end: RGB, t: float) -> RGB:
    """Linear RGB blend; ``t`` is clamped to 0.0 … 1.0."""
    t = max(0.0, min(1.0, t))
    return (
        int(start[0] + (end[0] - start[0]) * t),
        int(start[1] + (end[1] - start[1]) * t),
        int(start[2] + (end[2] - start[2]) * t),
    )


def mix(*stops: RGB, t: float) -> RGB:
    """Sample a multi-stop ramp at ``t`` — for gradients that turn, not just fade."""
    if not stops:
        raise ValueError("mix() needs at least one stop")
    if len(stops) == 1:
        return stops[0]
    t = max(0.0, min(1.0, t))
    scaled = t * (len(stops) - 1)
    index = min(int(scaled), len(stops) - 2)
    return interpolate_color(stops[index], stops[index + 1], scaled - index)


def ramp(stops: list[RGB], count: int) -> list[RGB]:
    """``count`` evenly spaced samples across a ramp."""
    if count <= 1:
        return [mix(*stops, t=0.0) for _ in range(max(0, count))]
    return [mix(*stops, t=index / (count - 1)) for index in range(count)]


def visible_width(text: str) -> int:
    """Cells in a string that may contain ANSI escapes."""
    from xli.ui.text import display_width

    return display_width(_ANSI.sub("", text))


# ------------------------------------------------------------------ terminal
def color_depth() -> str:
    """What this terminal can do: ``truecolor`` | ``256`` | ``16`` | ``none``."""
    if os.environ.get("NO_COLOR"):
        return "none"
    if os.environ.get("XLI_FORCE_COLOR_DEPTH"):
        forced = os.environ["XLI_FORCE_COLOR_DEPTH"].lower()
        if forced in ("truecolor", "24bit", "256", "16", "none"):
            return "truecolor" if forced in ("truecolor", "24bit") else forced
    colorterm = (os.environ.get("COLORTERM") or "").lower()
    if "truecolor" in colorterm or "24bit" in colorterm:
        return "truecolor"
    term = (os.environ.get("TERM") or "").lower()
    if "truecolor" in term or "direct" in term or "kitty" in term or "wezterm" in term:
        return "truecolor"
    if term in ("dumb", "", "unknown"):
        return "16"
    if "256" in term or "color" in term:
        return "256"
    return "16"


def _nearest_256(rgb: RGB) -> int:
    """Map an RGB triple onto the xterm-256 cube + greyscale ramp."""
    r, g, b = (max(0, min(255, int(c))) for c in rgb)
    if abs(r - g) < 8 and abs(g - b) < 8:
        # Greyscale ramp: 232..255, values 8, 18, … 238.
        if r < 8:
            return 16
        if r > 238:
            return 231
        return 232 + round((r - 8) / 10)
    steps = (0, 95, 135, 175, 215, 255)

    def bucket(value: int) -> int:
        return min(range(6), key=lambda i: abs(steps[i] - value))

    return 16 + 36 * bucket(r) + 6 * bucket(g) + bucket(b)


def _nearest_16(rgb: RGB) -> int:
    """Nearest of the 8 ANSI colours (plus bright variants), as a code 30..37/90..97."""
    table = (
        (0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16),
        (36, 114, 200), (188, 63, 188), (17, 168, 205), (229, 229, 229),
        (102, 102, 102), (241, 76, 76), (35, 209, 139), (245, 245, 67),
        (59, 142, 234), (214, 112, 214), (41, 184, 219), (255, 255, 255),
    )
    index = min(
        range(16),
        key=lambda i: sum((table[i][channel] - rgb[channel]) ** 2 for channel in range(3)),
    )
    return 30 + index if index < 8 else 82 + index


def fg(rgb: RGB, *, depth: str | None = None) -> str:
    """Foreground escape for a colour at the terminal's depth."""
    depth = depth or color_depth()
    if depth == "none":
        return ""
    if depth == "truecolor":
        return f"\033[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    if depth == "256":
        return f"\033[38;5;{_nearest_256(rgb)}m"
    return f"\033[{_nearest_16(rgb)}m"


def bg(rgb: RGB, *, depth: str | None = None) -> str:
    depth = depth or color_depth()
    if depth == "none":
        return ""
    if depth == "truecolor":
        return f"\033[48;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    if depth == "256":
        return f"\033[48;5;{_nearest_256(rgb)}m"
    index = _nearest_16(rgb) - 30
    if index < 8:
        return f"\033[{40 + index}m"
    return f"\033[{100 + index - 8}m"


def xterm256_index(rgb: RGB) -> int:
    """Public helper for the curses painter, which needs the pair number."""
    return _nearest_256(rgb)


# ------------------------------------------------------------------ gradients
@dataclass(frozen=True)
class Glow:
    """A named ramp, so "the accent" is one object instead of four tuples."""

    stops: tuple[RGB, ...]
    bold: bool = False

    def sample(self, count: int, *, tick: int = 0, speed: float = 0.06) -> list[RGB]:
        """``count`` colours; ``tick`` shifts the phase so the ramp travels."""
        if count <= 0:
            return []
        shift = (tick * speed) % 1.0
        return [
            mix(*self.stops, t=((index / max(count - 1, 1)) + shift) % 1.0)
            for index in range(count)
        ]


#: The house gradient: deep violet → purple → neon magenta.
ACCENT_GLOW = Glow((XLI_DARK_VIOLET, XLI_PURPLE, XLI_NEON_MAGENTA))
#: Heading ramps, one per level — hierarchy you can see in a screenshot.
H1_GLOW = Glow((XLI_PURPLE, XLI_NEON_MAGENTA, XLI_GLOW_WHITE))
H2_GLOW = Glow((XLI_DARK_VIOLET, XLI_PURPLE, XLI_NEON_MAGENTA))
H3_GLOW = Glow((XLI_PURPLE, XLI_NEON_MAGENTA))
#: Status colours, for text that means something rather than decorating.
GOOD_GLOW = Glow((XLI_MINT, XLI_MINT))
WARN_GLOW = Glow((XLI_AMBER, XLI_AMBER))
BAD_GLOW = Glow((XLI_RED, XLI_RED))

#: Markdown/UI style name -> ramp used when the painter wants a travelling
#: glow. Painters ask :func:`style_glow` rather than hardcoding colours, so the
#: CLI and the TUI never disagree about what "heading" looks like.
STYLE_GLOW: dict[str, Glow] = {
    "heading": H1_GLOW,
    "heading2": H2_GLOW,
    "heading3": H3_GLOW,
    "accent": ACCENT_GLOW,
    "good": GOOD_GLOW,
    "warn": WARN_GLOW,
    "bad": BAD_GLOW,
}


def style_glow(style: str) -> Glow | None:
    """The ramp a style should be painted with, or None for flat colour."""
    return STYLE_GLOW.get(style)


def style_colors(style: str, count: int, *, tick: int = 0) -> list[RGB] | None:
    """Per-character colours for a glowing style, or None to paint it flat."""
    glow = STYLE_GLOW.get(style)
    if glow is None or count <= 0:
        return None
    return glow.sample(count, tick=tick)


def gradient_text(
    text: str,
    start: RGB = XLI_DARK_VIOLET,
    end: RGB = XLI_NEON_MAGENTA,
    bold: bool = False,
    *,
    enabled: bool | None = None,
    depth: str | None = None,
    tick: int = 0,
    reset: bool = True,
) -> str:
    """Per-character gradient across ``text``.

    Newlines are preserved without colour so multi-line banners keep their
    layout. ``tick`` shifts the ramp, which is how a CLI banner animates.
    """
    if not text:
        return text
    if enabled is None:
        enabled = color_depth() != "none"
    if not enabled or color_depth() == "none":
        return text
    depth = depth or color_depth()

    glow = Glow((start, end))
    printable = sum(1 for char in text if char != "\n")
    colors = glow.sample(max(printable, 1), tick=tick)

    out: list[str] = [BOLD] if bold else []
    index = 0
    last: str = ""
    for char in text:
        if char == "\n":
            out.append((RESET if reset else "") + "\n")
            # A reset cancels the bold face too; re-apply it for the next line.
            if bold and reset:
                out.append(BOLD)
            last = ""
            continue
        code = fg(colors[index], depth=depth)
        if code != last:
            out.append(code)
            last = code
        out.append(char)
        index += 1
    if reset:
        out.append(RESET)
    return "".join(out)


def gradient_rule(
    width: int,
    *,
    tick: int = 0,
    char: str = "─",
    enabled: bool | None = None,
    depth: str | None = None,
    cap: str = "",
) -> str:
    """A thin rule with a travelling bright spot — the animated separator.

    Unlike a plain fade, the brightness *moves* with ``tick``, which is what
    makes a static screen feel alive without any character ever changing place.
    """
    if width <= 0:
        return ""
    if enabled is None:
        enabled = color_depth() != "none"
    if not enabled or color_depth() == "none":
        return char * width
    depth = depth or color_depth()

    spot = int((math.sin(tick * 0.16) + 1) / 2 * max(0, width - 1))
    out: list[str] = []
    last = ""
    for index in range(width):
        distance = abs(index - spot)
        if distance == 0:
            color = XLI_GLOW_WHITE
        elif distance <= 2:
            color = XLI_NEON_MAGENTA
        elif distance <= 5:
            color = XLI_PURPLE
        else:
            color = mix(XLI_DARK_VIOLET, XLI_PURPLE, t=index / max(width - 1, 1))
        code = fg(color, depth=depth)
        if code != last:
            out.append(code)
            last = code
        out.append(char)
    out.append(RESET)
    return "".join(out) + (cap if cap else "")


def glow_title(text: str, *, tick: int = 0, icon: str = "◈", enabled: bool | None = None) -> str:
    """The one-line banner: ``◈ XLI`` in the house gradient.

    The dot pulses between magenta and white, so the same call at a different
    ``tick`` looks like it is breathing.
    """
    if enabled is None:
        enabled = color_depth() != "none"
    if not enabled or color_depth() == "none":
        return text
    pulse = (math.sin(tick * 0.2) + 1) / 2
    icon_color = interpolate_color(XLI_PURPLE, XLI_GLOW_WHITE, pulse)
    head = f"{fg(icon_color)}{BOLD}{icon}{RESET}"
    return f"{head} {gradient_text(text, *ACCENT_GLOW.stops[:2], True, tick=tick)}"


def box(
    lines: list[str],
    *,
    width: int | None = None,
    title: str = "",
    tick: int = 0,
    enabled: bool | None = None,
    depth: str | None = None,
) -> str:
    """A thin violet box with a glowing animated frame.

    Content is padded to the widest line (ANSI-aware) so the right border is
    straight — the old version used ``len()`` and the frame leaned right
    whenever a line contained an escape.
    """
    if not lines and not title:
        return ""
    if enabled is None:
        enabled = color_depth() != "none"
    if not enabled or color_depth() == "none":
        inner = max([visible_width(line) for line in lines] or [0])
        inner = max(inner, visible_width(title))
        out = ["┌" + "─" * (inner + 2) + "┐"]
        if title:
            out.append("│ " + title.ljust(inner) + " │")
            out.append("├" + "─" * (inner + 2) + "┤")
        out += ["│ " + line.ljust(inner) + " │" for line in lines]
        out.append("└" + "─" * (inner + 2) + "┘")
        return "\n".join(out)

    depth = depth or color_depth()
    inner = max([visible_width(line) for line in lines] or [0])
    inner = max(inner, visible_width(title))
    if width is not None:
        inner = max(inner, width - 4)

    spot = int((math.sin(tick * 0.12) + 1) / 2 * max(0, inner + 1))

    def frame(char: str, left: str, right: str) -> str:
        cells: list[str] = []
        for index in range(inner + 2):
            distance = abs(index - (spot + 1))
            if distance == 0:
                color = XLI_GLOW_WHITE
            elif distance <= 2:
                color = XLI_NEON_MAGENTA
            elif distance <= 5:
                color = XLI_PURPLE
            else:
                color = XLI_DARK_VIOLET
            cells.append(f"{fg(color, depth=depth)}{char}")
        return f"{fg(XLI_PURPLE, depth=depth)}{left}{''.join(cells)}{right}{RESET}"

    out: list[str] = [frame("─", "┌", "┐")]
    if title:
        out.append(
            f"{fg(XLI_PURPLE, depth=depth)}│{RESET} "
            f"{gradient_text(title, *H1_GLOW.stops[:2], tick=tick)}"
            f"{' ' * max(0, inner - visible_width(title))} "
            f"{fg(XLI_PURPLE, depth=depth)}│{RESET}"
        )
        out.append(frame("─", "├", "┤"))
    for line in lines:
        pad = " " * max(0, inner - visible_width(line))
        out.append(f"{fg(XLI_PURPLE, depth=depth)}│{RESET} {line}{pad} {fg(XLI_PURPLE, depth=depth)}│{RESET}")
    out.append(frame("─", "└", "┘"))
    return "\n".join(out)


def paint(
    text: str,
    color: "RGB | str | None" = None,
    *,
    bold: bool = False,
    dim: bool = False,
    enabled: bool | None = None,
    depth: str | None = None,
) -> str:
    """One-off colouring with the same depth rules as everything else."""
    if enabled is None:
        enabled = color_depth() != "none"
    if not enabled or color_depth() == "none":
        return text
    depth = depth or color_depth()
    prefix = ""
    if bold:
        prefix += BOLD
    if dim:
        prefix += DIM
    rgb = as_rgb(color)
    if rgb is not None:
        prefix += fg(rgb, depth=depth)
    return f"{prefix}{text}{RESET}" if prefix else text


__all__ = [
    "ACCENT_GLOW",
    "BAD_GLOW",
    "Glow",
    "GOOD_GLOW",
    "H1_GLOW",
    "H2_GLOW",
    "H3_GLOW",
    "STYLE_GLOW",
    "WARN_GLOW",
    "XLI_AMBER",
    "XLI_BLUE",
    "XLI_DARK_VIOLET",
    "XLI_GLOW_WHITE",
    "XLI_MINT",
    "XLI_NEON_MAGENTA",
    "XLI_PURPLE",
    "XLI_RED",
    "as_rgb",
    "bg",
    "box",
    "color_depth",
    "fg",
    "glow_title",
    "gradient_rule",
    "gradient_text",
    "hex_to_rgb",
    "interpolate_color",
    "mix",
    "paint",
    "ramp",
    "rgb_to_hex",
    "style_colors",
    "style_glow",
    "visible_width",
    "xterm256_index",
]
