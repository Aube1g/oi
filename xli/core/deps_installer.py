#!/usr/bin/env python3
"""
XLI Dependency Installer — interactive dependency management.

Provides a cross-platform installer that works through UiPort,
so it renders correctly in CLI (terminal), TUI (curses), and Neovim.

Features:
- Describes each package before installing (what it is, why needed)
- Asks for user confirmation
- Animated progress bar with stages
- Graceful fallback if UI cannot ask questions
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xli.ui.port import UiPort


# ==========================================================================
# DATA MODEL
# ==========================================================================

@dataclass
class DependencyInfo:
    """Metadata about an optional dependency."""
    name: str                          # Python import name (e.g. "httpx")
    pip_package: str = ""              # pip install name (defaults to name)
    version: str = ""                  # Minimum version constraint
    description: str = ""              # What this package does
    why_needed: str = ""               # Why XLI needs it
    required_by: list[str] = field(default_factory=list)  # Which features need it

    def __post_init__(self):
        if not self.pip_package:
            self.pip_package = self.name

    @property
    def install_spec(self) -> str:
        """pip-compatible install specifier."""
        if self.version:
            return f"{self.pip_package}>={self.version}"
        return self.pip_package

    def is_installed(self) -> bool:
        """Check if the package is importable."""
        try:
            importlib.import_module(self.name)
            return True
        except ImportError:
            return False


# ==========================================================================
# KNOWN DEPENDENCIES REGISTRY
# ==========================================================================

KNOWN_DEPENDENCIES: dict[str, DependencyInfo] = {
    "httpx": DependencyInfo(
        name="httpx",
        description="Modern async HTTP client for Python",
        why_needed="Web search, fetching pages, API calls to SearXNG instances",
        required_by=["web_search", "fetch_page"],
    ),
    "rich": DependencyInfo(
        name="rich",
        description="Rich text and beautiful formatting in the terminal",
        why_needed="Enhanced TUI rendering, colored trees, tables, progress bars",
        required_by=["tui", "graph"],
    ),
    "textual": DependencyInfo(
        name="textual",
        description="Modern TUI framework for Python",
        why_needed="Full-screen interactive interface with widgets",
        required_by=["tui-advanced"],
    ),
    "pyfiglet": DependencyInfo(
        name="pyfiglet",
        description="ASCII art banner generator",
        why_needed="Startup banners and decorative headers",
        required_by=["ui-decorations"],
    ),
    "watchdog": DependencyInfo(
        name="watchdog",
        description="Filesystem event monitoring",
        why_needed="Auto-reload when files change during development",
        required_by=["dev-mode"],
    ),
}


# ==========================================================================
# INSTALLER ENGINE
# ==========================================================================

class InstallStage:
    """Named stages of the installation process."""
    ANALYZE = "analyze"
    DESCRIBE = "describe"
    CONFIRM = "confirm"
    DOWNLOAD = "download"
    INSTALL = "install"
    VERIFY = "verify"


class DepsInstaller:
    """Interactive dependency installer using UiPort for all I/O."""

    # Progress bar characters
    BAR_FILL = "█"
    BAR_EMPTY = "░"
    BAR_WIDTH = 30

    # Stage weights (must sum to 100)
    STAGE_WEIGHTS = {
        InstallStage.ANALYZE: 10,
        InstallStage.DESCRIBE: 10,
        InstallStage.CONFIRM: 5,
        InstallStage.DOWNLOAD: 35,
        InstallStage.INSTALL: 30,
        InstallStage.VERIFY: 10,
    }

    def __init__(self, ui: UiPort, registry: dict[str, DependencyInfo] | None = None):
        self.ui = ui
        self.registry = registry or KNOWN_DEPENDENCIES

    # ------------------------------------------------------------------ public

    def check_and_install(self, packages: list[str]) -> dict[str, bool]:
        """Check which packages are missing and offer to install them.

        Returns a dict mapping package_name -> success (True/False/Skipped).
        """
        results: dict[str, bool] = {}

        # Stage 1: Analyze
        self._report_stage(InstallStage.ANALYZE, 0, "Scanning dependencies...")
        missing = []
        for pkg_name in packages:
            dep = self.registry.get(pkg_name)
            if dep is None:
                dep = DependencyInfo(name=pkg_name, description="Unknown package")
            if not dep.is_installed():
                missing.append(dep)
            else:
                results[pkg_name] = True

        self._report_stage(InstallStage.ANALYZE, 100, f"Found {len(missing)} missing")

        if not missing:
            self.ui.notify("All dependencies are installed.", level="info")
            return results

        # Stage 2: Describe
        self._report_stage(InstallStage.DESCRIBE, 0, "Preparing descriptions...")
        for i, dep in enumerate(missing):
            pct = int((i + 1) / len(missing) * 100)
            self._report_stage(InstallStage.DESCRIBE, pct, f"Reviewing {dep.name}...")
            self._show_dependency_info(dep)

        # Stage 3: Confirm
        self._report_stage(InstallStage.CONFIRM, 0, "Waiting for confirmation...")
        if not self.ui.can_ask:
            self.ui.notify(
                "Cannot ask for confirmation in this mode. Skipping install.",
                level="warning",
            )
            for dep in missing:
                results[dep.name] = False
            return results

        pkg_list = ", ".join(d.name for d in missing)
        confirmed = self.ui.confirm(
            f"Install {len(missing)} package(s): {pkg_list}?"
        )
        self._report_stage(InstallStage.CONFIRM, 100, "Confirmed" if confirmed else "Declined")

        if not confirmed:
            for dep in missing:
                results[dep.name] = False
            return results

        # Stage 4-6: Download, Install, Verify (per package)
        for dep in missing:
            success = self._install_single(dep)
            results[dep.name] = success

        # Final summary
        ok_count = sum(1 for v in results.values() if v)
        fail_count = sum(1 for v in results.values() if not v)
        self.ui.display(f"\n📦 Installation complete: {ok_count} succeeded, {fail_count} failed/skipped")

        return results

    def ensure_dependency(self, name: str) -> bool:
        """Convenience: ensure a single dependency is available."""
        results = self.check_and_install([name])
        return results.get(name, False)

    # ------------------------------------------------------------------ internal

    def _show_dependency_info(self, dep: DependencyInfo) -> None:
        """Display formatted info about a dependency."""
        lines = [
            f"┌─ 📦 {dep.name} {'(' + dep.version + ')' if dep.version else ''}",
            f"│  Description : {dep.description or 'N/A'}",
            f"│  Why needed  : {dep.why_needed or 'Required by XLI'}",
            f"│  Used by     : {', '.join(dep.required_by) if dep.required_by else 'core'}",
            f"│  pip install : {dep.install_spec}",
            f"└{'─' * 50}",
        ]
        self.ui.display("\n".join(lines))

    def _install_single(self, dep: DependencyInfo) -> bool:
        """Install a single package with animated progress."""
        self._report_stage(InstallStage.DOWNLOAD, 0, f"Downloading {dep.name}...")

        cmd = [
            sys.executable, "-m", "pip", "install",
            dep.install_spec,
            "--quiet",
            "--disable-pip-version-check",
        ]

        try:
            # Simulate download progress (pip --quiet doesn't give us bytes)
            for pct in range(0, 101, 20):
                self._report_stage(InstallStage.DOWNLOAD, pct, f"Downloading {dep.name}...")

            self._report_stage(InstallStage.INSTALL, 0, f"Installing {dep.name}...")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )

            for pct in range(0, 101, 25):
                self._report_stage(InstallStage.INSTALL, pct, f"Installing {dep.name}...")

            if result.returncode != 0:
                error_msg = result.stderr.strip()[:200] if result.stderr else "unknown error"
                self.ui.notify(f"Failed to install {dep.name}: {error_msg}", level="error")
                return False

            # Verify
            self._report_stage(InstallStage.VERIFY, 50, f"Verifying {dep.name}...")
            if dep.is_installed():
                self._report_stage(InstallStage.VERIFY, 100, f"✅ {dep.name} installed!")
                self.ui.notify(f"{dep.name} installed successfully", level="info")
                return True
            else:
                self.ui.notify(f"{dep.name} installed but cannot be imported", level="error")
                return False

        except subprocess.TimeoutExpired:
            self.ui.notify(f"Installation of {dep.name} timed out", level="error")
            return False
        except FileNotFoundError:
            self.ui.notify("pip not found. Is Python installed correctly?", level="error")
            return False
        except Exception as e:
            self.ui.notify(f"Error installing {dep.name}: {e}", level="error")
            return False

    def _report_stage(self, stage: str, stage_pct: int, message: str) -> None:
        """Calculate overall progress and report to UI."""
        # Calculate cumulative progress across stages
        stage_order = [
            InstallStage.ANALYZE,
            InstallStage.DESCRIBE,
            InstallStage.CONFIRM,
            InstallStage.DOWNLOAD,
            InstallStage.INSTALL,
            InstallStage.VERIFY,
        ]

        total = 0
        for s in stage_order:
            weight = self.STAGE_WEIGHTS.get(s, 0)
            if s == stage:
                total += int(weight * stage_pct / 100)
                break
            total += weight

        total = min(total, 100)

        # Build visual progress bar
        filled = int(self.BAR_WIDTH * total / 100)
        bar = self.BAR_FILL * filled + self.BAR_EMPTY * (self.BAR_WIDTH - filled)

        display_msg = f"[{bar}] {total:3d}% │ {stage}: {message}"
        self.ui.progress(total, display_msg)
