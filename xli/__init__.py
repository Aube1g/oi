"""xli — a local-first AI coding assistant.

The package version lives here, not in the CLI. It used to be defined in
xli/cli.py, which meant xli/kernel/methods.py imported the CLI just to report a
version string over the wire — a dependency pointing the wrong way, and an
import cycle (xli.cli -> xli.kernel.methods -> xli.cli).

Keep this module import-cheap: everything the version is compared against
(pyproject.toml) is checked by tests/test_package_metadata.py, so there is no
need to duplicate it anywhere else.
"""

__version__ = "6.0.0"
VERSION = __version__

__all__ = ["VERSION", "__version__"]
