#!/usr/bin/env python3
"""One definition of where xli keeps its files.

Every module used to compute `Path.home() / ".xli" / <something>` for itself.
`Config.user_path()` was the only one that consulted XLI_CONFIG_DIR, so setting
that variable moved the config file and nothing else — cache.db, memory.db,
skills.db, snapshots/, queue/, logs/, team_inbox/, xpi_state.json and the rest
all stayed in the real home directory. Verified before the fix: with
XLI_CONFIG_DIR set, 5 of 5 sampled paths still resolved under $HOME/.xli.

That makes the override actively misleading. It looks like "put xli's state
here", and it does not. It also means two xli invocations pointed at different
config directories silently share a cache, a skills index and a memory
database — which is how one project's state ends up in another's.

This module deliberately imports nothing from xli. xli.core.logger is pulled in
by almost everything, so a helper that imported it would create a cycle the
moment logger needed a path.

The returned directory is created on demand by callers, not here: importing a
module should not write to disk.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Name of the per-user directory when no override is set.
DIRNAME = ".xli"

#: Environment variable that relocates the whole directory.
ENV_VAR = "XLI_CONFIG_DIR"


def xli_home() -> Path:
    """The directory holding xli's per-user state.

    Honours $XLI_CONFIG_DIR, falling back to ~/.xli. Callers should treat this
    as read-only configuration and derive their own paths from it, rather than
    reaching for Path.home() directly.
    """
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / DIRNAME


def xli_path(*parts: str) -> Path:
    """A path inside xli_home(), e.g. xli_path("cache.db")."""
    return xli_home().joinpath(*parts)


__all__ = ["DIRNAME", "ENV_VAR", "xli_home", "xli_path"]
