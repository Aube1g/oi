"""
XPI — XLI's internal plugin system.

In-process plugins dropped into `~/.xli/xpi/<name>/`, each a subclass of
`XpiPlugin` with lifecycle hooks. Distinct from MCP servers (separate
processes) and skills (markdown guidance).
"""

from xli.xpi.base import XpiPlugin
from xli.xpi.bridge import XpiBridge
from xli.xpi.manager import (
    KNOWN_HOOKS,
    XPI_DIR,
    DispatchReport,
    XpiInfo,
    XpiManager,
    get_xpi_manager,
)
from xli.xpi.registry import XpiEntry, XpiRegistry
from xli.xpi.state import XpiState, get_xpi_state

__all__ = [
    "XpiPlugin",
    "XpiManager",
    "XpiInfo",
    "DispatchReport",
    "get_xpi_manager",
    "KNOWN_HOOKS",
    "XPI_DIR",
    "XpiRegistry",
    "XpiEntry",
    "XpiState",
    "get_xpi_state",
    "XpiBridge",
]
