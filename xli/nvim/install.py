#!/usr/bin/env python3
"""
XLI Neovim installer — copy the plugin into a Neovim config.

Two ways to use the plugin, and this only exists for the second:

  1. a plugin manager, pointing at the `nvim/` directory in this repository —
     nothing to install, nothing to update
  2. no plugin manager — `xli nvim install` copies the files into your config

The installer is deliberately boring and safe: it refuses to overwrite an
existing installation unless told to, and it writes a small `require('xli')`
snippet only into a file it creates itself, never into an init.lua you already
own.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

#: Shipped plugin root. It lives *inside* the package so it travels with the
#: wheel — an installed `xli nvim install` must work without a source checkout.
PLUGIN_SOURCE = Path(__file__).resolve().parent / "plugin_root"

#: The lua module tree and the plugin/ loader, relative to PLUGIN_SOURCE.
PAYLOAD_DIRS = ("lua/xli", "plugin")

SNIPPET = """\
-- Added by `xli nvim install`. Remove this block to uninstall.
require('xli').setup({
  start_kernel = true,
})
"""

SNIPPET_MARKER = "Added by `xli nvim install`"


def nvim_config_dir() -> Path:
    """Where Neovim looks for user config on this platform."""
    override = os.environ.get("XDG_CONFIG_HOME")
    if override:
        return Path(override) / "nvim"
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "nvim"
    return Path.home() / ".config" / "nvim"


def install_plugin(
    *,
    target: Path | None = None,
    force: bool = False,
    write_snippet: bool = True,
) -> dict[str, Any]:
    """Copy the plugin into `target` (default: the Neovim config directory).

    Returns a report; `ok` is False only when the source is missing or a file
    would have been overwritten without `force`.
    """
    if not PLUGIN_SOURCE.is_dir():
        return {"ok": False, "error": f"plugin source not found: {PLUGIN_SOURCE}"}

    destination = Path(target) if target else nvim_config_dir()
    copied: list[str] = []
    skipped: list[str] = []
    blocked: list[str] = []

    for relative_dir in PAYLOAD_DIRS:
        source_dir = PLUGIN_SOURCE / relative_dir
        if not source_dir.is_dir():
            continue
        for source in sorted(source_dir.rglob("*")):
            if source.is_dir():
                continue
            rel = source.relative_to(PLUGIN_SOURCE)
            dest = destination / rel
            if dest.exists() and not force:
                blocked.append(str(rel))
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            copied.append(str(rel))

    if blocked and not force:
        return {
            "ok": False,
            "target": str(destination),
            "copied": copied,
            "blocked": blocked,
            "error": "existing files would be overwritten — pass force=True",
        }

    snippet_path: str | None = None
    if write_snippet:
        snippet_path = _write_snippet(destination)

    return {
        "ok": True,
        "target": str(destination),
        "copied": copied,
        "skipped": skipped,
        "snippet": snippet_path,
        "next": "restart Neovim, then run :XliStatus",
    }


def _write_snippet(config_dir: Path) -> str | None:
    """Add the setup() call to a dedicated file, never to the user's init.lua."""
    plugin_dir = config_dir / "plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    snippet = plugin_dir / "xli-init.lua"

    if snippet.exists():
        existing = snippet.read_text(encoding="utf-8")
        if SNIPPET_MARKER in existing:
            return str(snippet)  # already ours, leave it alone

    # Appending is the safe default: a user's own xli config must survive.
    with open(snippet, "a", encoding="utf-8") as handle:
        handle.write("\n" + SNIPPET)
    return str(snippet)


def uninstall_plugin(*, target: Path | None = None) -> dict[str, Any]:
    """Remove what the installer copied, and only that."""
    destination = Path(target) if target else nvim_config_dir()
    removed: list[str] = []

    for relative in ("lua/xli", "plugin/xli.lua", "plugin/xli-init.lua"):
        path = destination / relative
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed.append(relative)
        elif path.is_file():
            path.unlink(missing_ok=True)
            removed.append(relative)

    return {"ok": True, "target": str(destination), "removed": removed}


def plugin_files() -> list[str]:
    """Every file the installer would copy — used by tests and by `--dry-run`."""
    if not PLUGIN_SOURCE.is_dir():
        return []
    return sorted(
        str(path.relative_to(PLUGIN_SOURCE))
        for relative_dir in PAYLOAD_DIRS
        for path in (PLUGIN_SOURCE / relative_dir).rglob("*")
        if path.is_file()
    )
