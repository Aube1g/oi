#!/usr/bin/env python3
"""XLI agent identity — a colour and a badge per (sub-)agent.

A delegated sub-agent used to appear as a bracketed name in front of somebody
else's transcript lines: ``[reviewer] xli`` — the same violet, the same word,
for a different actor. With several delegates it was impossible to tell at a
glance who was speaking, or that anything had been delegated at all.

This module gives each agent a stable colour (from the name, so it survives
restarts) and a badge the front ends can print. The colour is returned as a
``#RRGGBB`` *style name*: the ANSI painter understands hex styles directly,
and the curses painter resolves them to a 256-colour pair on demand, so both
paint the same colour without a second palette table.
"""

from __future__ import annotations

from xli.ui.gradient import (
    XLI_AMBER,
    XLI_BLUE,
    XLI_MINT,
    XLI_NEON_MAGENTA,
    XLI_PURPLE,
    XLI_RED,
    rgb_to_hex,
)

#: The palette a delegate can be drawn in. Violet first — it is the house
#: colour — then colours that stay distinguishable next to it.
AGENT_COLORS: tuple[tuple[int, int, int], ...] = (
    XLI_PURPLE,
    XLI_MINT,
    XLI_AMBER,
    XLI_BLUE,
    XLI_NEON_MAGENTA,
    XLI_RED,
)

#: Named colours an AgentSpec may ask for by name.
NAMED: dict[str, tuple[int, int, int]] = {
    "accent": XLI_PURPLE,
    "good": XLI_MINT,
    "warn": XLI_AMBER,
    "bad": XLI_RED,
    "blue": XLI_BLUE,
    "magenta": XLI_NEON_MAGENTA,
}


def agent_color(name: str, hint: str = "") -> str:
    """A stable ``#RRGGBB`` for an agent.

    ``hint`` is the spec's ``colour`` field when it has one; unknown or empty
    hints fall back to hashing the name, so two delegates never collide by
    accident and the same delegate is the same colour tomorrow.
    """
    if hint:
        rgb = NAMED.get(hint.strip().lower())
        if rgb is None and hint.startswith("#") and len(hint) == 7:
            return hint.upper()
        if rgb is not None:
            return rgb_to_hex(rgb)
    if not name:
        return rgb_to_hex(AGENT_COLORS[0])
    digest = 0
    for char in name:
        digest = (digest * 131 + ord(char)) % 100_003
    return rgb_to_hex(AGENT_COLORS[digest % len(AGENT_COLORS)])


def agent_style(name: str, hint: str = "") -> str:
    """The style key the painters understand for this agent."""
    return agent_color(name, hint)


def agent_badge(name: str, hint: str = "", *, kind: str = "agent") -> tuple[str, str]:
    """``("◆ reviewer", "#9D4EDD")`` — text and style for a transcript line."""
    glyph = {"agent": "◆", "result": "◇", "tool": "⚙"}.get(kind, "◆")
    return (f"{glyph} {name}", agent_color(name, hint))


def agent_hint_for(name: str, project_root: str | None = None) -> str:
    """Look up the spec's colour hint, if the registry can tell us.

    Kept best-effort: a broken agent folder must not break the transcript, so
    every failure path returns "" and the caller falls back to hashing.
    """
    try:
        from xli.agents.registry import get_registry

        registry = get_registry(project_root=project_root) if project_root else get_registry()
        spec = registry.get(name)
        return getattr(spec, "colour", "") or ""
    except Exception:  # noqa: BLE001 - identity is cosmetic, never fatal
        return ""


__all__ = ["AGENT_COLORS", "NAMED", "agent_badge", "agent_color", "agent_hint_for", "agent_style"]
