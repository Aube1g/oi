#!/usr/bin/env python3
"""
XLI entry point — `python -m xli` and the `xli` console script both land here.

The real implementation lives in xli.cli; this module only bootstraps it, so
there is exactly one place that parses arguments.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the repository importable when run straight from a checkout, before the
# package has been installed.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def main(argv=None) -> int:
    from xli.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
