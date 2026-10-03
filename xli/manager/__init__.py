#!/usr/bin/env python3
"""
XLI Manager — configuration and kernel build control.

This is the layer `xli config` and `xli kernel` drive: everything the user can
tune, plus the optional Cython compilation of xli.core.
"""

from xli.manager.config import (
    CHOICES,
    DEFAULTS,
    RANGES,
    Config,
    ConfigError,
    get_config,
    reset_config,
)
from xli.manager.kernel_build import (
    BUILD_DIR,
    CKERNEL_DIR,
    BuildReport,
    Manifest,
    Preflight,
    build,
    clean,
    discover_targets,
    is_usable,
    preflight,
    source_hash,
    status,
)

__all__ = [
    "Config",
    "ConfigError",
    "DEFAULTS",
    "CHOICES",
    "RANGES",
    "get_config",
    "reset_config",
    "build",
    "clean",
    "status",
    "preflight",
    "Preflight",
    "BuildReport",
    "Manifest",
    "discover_targets",
    "source_hash",
    "is_usable",
    "CKERNEL_DIR",
    "BUILD_DIR",
]
