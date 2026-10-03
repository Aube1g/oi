#!/usr/bin/env python3
"""
XLI guides — in-tree walkthroughs for the workflows the CLI exposes.

Guides live inside the package (not in docs/) so `xli guides` can read them
from any install, including a phone where there is no source checkout. Each
guide is one markdown file; the command lists the directory and renders a
guide through the same markdown pipeline the assistant answers use.
"""

from __future__ import annotations

from pathlib import Path

GUIDES_DIR = Path(__file__).parent


def list_guides() -> list[tuple[str, str]]:
    """(stem, first heading) for every guide, sorted by filename."""
    guides: list[tuple[str, str]] = []
    for path in sorted(GUIDES_DIR.glob("*.md")):
        title = path.stem
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                title = stripped[2:].strip()
                break
        guides.append((path.stem, title))
    return guides


def read_guide(name: str) -> str | None:
    """Guide text by stem, or None. Names are exact: no guessing."""
    path = GUIDES_DIR / f"{name}.md"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")
