#!/usr/bin/env python3
"""XLI Gradient Engine — compatibility shim.

The real engine lives in :mod:`xli.ui.gradient`, next to the other painters
(the ANSI painter, the curses palette, the charts) so that colour decisions
happen in one package. This module keeps the historical import path working:
``from xli.gradient import gradient_text`` is used by demos and by anyone who
scripted against the first version.

Everything here is a re-export — no logic, so the two can never disagree.
"""

from __future__ import annotations

from xli.ui.gradient import (  # noqa: F401
    ACCENT_GLOW,
    BOLD,
    H1_GLOW,
    H2_GLOW,
    H3_GLOW,
    RESET,
    STYLE_GLOW,
    XLI_DARK_VIOLET,
    XLI_GLOW_WHITE,
    XLI_NEON_MAGENTA,
    XLI_PURPLE,
    Glow,
    box,
    color_depth,
    fg,
    glow_title,
    gradient_rule,
    gradient_text,
    hex_to_rgb,
    interpolate_color,
    mix,
    ramp,
    rgb_to_hex,
    style_colors,
    style_glow,
    visible_width,
)

#: The old name for :func:`xli.ui.gradient.box`.
gradient_box = box


def ansi_fg(rgb: tuple[int, int, int]) -> str:
    """True-colour foreground escape, regardless of what the terminal claims.

    Kept because the first version always emitted 24-bit codes and scripts may
    depend on that exact byte sequence. New code should use ``fg()``, which
    degrades to 256 colours when the terminal cannot do 24-bit.
    """
    return f"\033[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


__all__ = [
    "ACCENT_GLOW",
    "BOLD",
    "Glow",
    "H1_GLOW",
    "H2_GLOW",
    "H3_GLOW",
    "RESET",
    "STYLE_GLOW",
    "XLI_DARK_VIOLET",
    "XLI_GLOW_WHITE",
    "XLI_NEON_MAGENTA",
    "XLI_PURPLE",
    "ansi_fg",
    "box",
    "color_depth",
    "fg",
    "glow_title",
    "gradient_box",
    "gradient_rule",
    "gradient_text",
    "hex_to_rgb",
    "interpolate_color",
    "mix",
    "ramp",
    "rgb_to_hex",
    "style_colors",
    "style_glow",
    "visible_width",
]
