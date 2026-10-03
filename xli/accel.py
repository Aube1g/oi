#!/usr/bin/env python3
"""
XLI Accel — transparent loading of the compiled kernel.

Installing the hook makes `from xli.core.diff_engine import DiffEngine` resolve
to the Cython extension in `xli/_ckernel/` when one exists *and* still matches
its source file. When it does not, the ordinary pure-Python module loads and
nothing else changes. No caller anywhere in the codebase has to know which one
it got.

The two guards are the whole point:

  * **hash match** — edit a .py after building and the stale .so is ignored,
    rather than silently running yesterday's logic
  * **interpreter match** — a .so built for cp311 is never loaded by cp312

Together they mean "the kernel is built" can never be a source of spooky
behaviour. Worst case it is simply not used.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys

from xli.manager.kernel_build import CKERNEL_DIR, Manifest, find_compiled, is_usable

CORE_PACKAGE = "xli.core"
HOOK_NAME = "xli-accel"


class KernelFinder(importlib.abc.MetaPathFinder):
    """Meta path finder redirecting xli.core.* to compiled extensions."""

    def __init__(self) -> None:
        self._manifest: Manifest | None = None
        self.loaded: dict[str, str] = {}

    @property
    def manifest(self) -> Manifest:
        if self._manifest is None:
            self._manifest = Manifest.load()
        return self._manifest

    def refresh(self) -> None:
        """Re-read the manifest, e.g. right after a build."""
        self._manifest = Manifest.load()

    def find_spec(self, fullname: str, path=None, target=None):
        if not fullname.startswith(CORE_PACKAGE + "."):
            return None
        stem = fullname.rsplit(".", 1)[-1]
        if not is_usable(stem, self.manifest):
            return None

        candidates = find_compiled(stem)
        if not candidates:
            return None

        loader = importlib.machinery.ExtensionFileLoader(fullname, str(candidates[0]))
        self.loaded[fullname] = str(candidates[0])
        return importlib.util.spec_from_loader(fullname, loader, origin=str(candidates[0]))


_HOOK: KernelFinder | None = None


def is_installed() -> bool:
    return _HOOK is not None


def install() -> KernelFinder:
    """Install the hook. Idempotent; returns the active finder."""
    global _HOOK
    if _HOOK is None:
        _HOOK = KernelFinder()
        # Front of sys.meta_path so we are consulted before the source loader.
        sys.meta_path.insert(0, _HOOK)
    else:
        _HOOK.refresh()
    return _HOOK


def uninstall() -> None:
    """Remove the hook and drop any modules it loaded, so pure Python takes over."""
    global _HOOK
    if _HOOK is None:
        return
    try:
        sys.meta_path.remove(_HOOK)
    except ValueError:
        pass
    for name in list(_HOOK.loaded):
        sys.modules.pop(name, None)
    _HOOK = None


def accelerated() -> list[str]:
    """Modules the hook would serve from the compiled kernel right now."""
    from xli.manager.kernel_build import discover_targets

    manifest = Manifest.load()
    return [p.stem for p in discover_targets() if is_usable(p.stem, manifest)]


def import_module(name: str):
    """Import a core module, using the hook when it is installed."""
    return importlib.import_module(f"{CORE_PACKAGE}.{name}" if "." not in name else name)


def auto_install() -> bool:
    """Install the hook only when something is actually compiled.

    Called at startup: a source-only checkout pays nothing, a built checkout
    gets the speed without being asked.
    """
    if not CKERNEL_DIR.is_dir() or not accelerated():
        return False
    install()
    return True
