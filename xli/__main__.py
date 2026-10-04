#!/usr/bin/env python3
"""
XLI entry point — `python -m xli` and the `xli` console script both land here.

The real implementation lives in xli.cli; this module only bootstraps it, so
there is exactly one place that parses arguments.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the repository importable when run straight from a checkout, before the
# package has been installed.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def main(argv=None) -> int:
    # `python -m xli --verbose ...` calls this with no arguments, so the real
    # command line has to be read here rather than defaulted to an empty list
    # (which silently dropped the flag).
    if argv is None:
        argv = sys.argv[1:]

    # Set before xli.cli is imported, because the loggers are built at import
    # time and capture the level in their handlers. A user running `xli` is
    # reading Russian output, not English INFO lines about internal wiring;
    # `XLI_LOG_CONSOLE_LEVEL=INFO` (or --verbose) brings them back, and the
    # full log is always in ~/.xli/logs/xli.log.
    os.environ.setdefault(
        "XLI_LOG_CONSOLE_LEVEL",
        "INFO" if "--verbose" in argv or os.environ.get("XLI_VERBOSE") else "WARNING",
    )
    if "--verbose" in argv:
        argv = [item for item in argv if item != "--verbose"]
        os.environ["XLI_LOG_CONSOLE_LEVEL"] = "INFO"

    from xli.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
