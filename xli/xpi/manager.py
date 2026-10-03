#!/usr/bin/env python3
"""
XPI Manager — loads, instantiates and dispatches internal plugins.

XPI ("XLI Plugin Interface") is xli's internal plugin system: small Python
packages dropped into `~/.xli/xpi/<name>/`, each with a `manifest.json` and a
class deriving from `xli.xpi.base.XpiPlugin`. Unlike MCP servers (separate
processes, remote) or skills (markdown guidance), an XPI plugin runs in-process
and gets lifecycle hooks — it can watch the agent work, keep state across UI
platforms, and extend what the frontends show.

Layout
------
    ~/.xli/xpi/
      my-plugin/
        manifest.json     {"name": "my-plugin", "version": "1.0", "main": "plugin.py"}
        plugin.py         class MyPlugin(XpiPlugin): ...

Isolation rules this file follows:

  * **One bad plugin never takes down the others.** Loading and hook dispatch
    are wrapped per plugin; failures are collected and reported, not raised.
  * **A hook that crashes is not silently swallowed.** The old `call_hook`
    returned None both for "no such hook" and "hook raised", so a caller could
    not tell them apart. `dispatch` returns a report instead.
  * **The singleton can be reset**, so tests (and `xli plugins reload`) can
    pick up changes without restarting the process.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from xli.paths import xli_path
from xli.core.logger import StructuredLogger
from xli.xpi.base import XpiPlugin
from xli.xpi.registry import XpiRegistry

logger = StructuredLogger("xli.xpi.manager")

XPI_DIR = xli_path("xpi")

#: Hooks the lifecycle knows about. A plugin may implement any subset.
KNOWN_HOOKS = (
    "on_load",
    "on_agent_start",
    "on_tool_call",
    "on_tool_result",
    "on_agent_end",
    "on_tui_mount",
    "on_nvim_attach",
    "on_headless_start",
    "on_unload",
)


@dataclass
class XpiInfo:
    """Metadata for one loaded plugin."""

    name: str
    version: str
    path: Path
    enabled: bool = True
    platform: str = "all"
    entry: str = ""
    loaded_at: datetime | None = None
    error: str = ""
    hooks: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "path": str(self.path),
            "enabled": self.enabled,
            "platform": self.platform,
            "entry": self.entry,
            "loaded_at": self.loaded_at.isoformat(timespec="seconds") if self.loaded_at else None,
            "error": self.error,
            "hooks": sorted(self.hooks),
        }


@dataclass
class DispatchReport:
    """What happened when a hook was broadcast."""

    hook: str
    called: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook": self.hook,
            "called": self.called,
            "errors": self.errors,
            "results": self.results,
        }


class XpiManager:
    """Discovers, loads and drives XPI plugins."""

    _instance: XpiManager | None = None

    def __new__(cls, *args: Any, **kwargs: Any) -> XpiManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, root: Path | None = None):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        self.root = Path(root) if root else XPI_DIR
        self.root.mkdir(parents=True, exist_ok=True)
        self.plugins: dict[str, XpiInfo] = {}
        self.instances: dict[str, XpiPlugin] = {}
        self._modules: dict[str, Any] = {}
        self.registry = XpiRegistry()

        self.load_all()
        logger.log_structured("INFO", "xpi.manager", f"{len(self.plugins)} plugin(s) available")

    @classmethod
    def reset(cls) -> None:
        """Drop the singleton so the next call re-scans from disk."""
        cls._instance = None

    # ------------------------------------------------------------- discovery
    def load_all(self) -> None:
        """Load every plugin directory that has a manifest."""
        if not self.root.is_dir():
            return
        for plugin_dir in sorted(self.root.iterdir()):
            if not plugin_dir.is_dir():
                continue
            manifest_path = plugin_dir / "manifest.json"
            if not manifest_path.exists():
                logger.log_structured(
                    "WARN", "xpi.manager", f"{plugin_dir.name} has no manifest.json — skipped"
                )
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.log_error("xpi.manager", f"bad manifest in {plugin_dir.name}", exc=exc)
                self.plugins[plugin_dir.name] = XpiInfo(
                    name=plugin_dir.name, version="?", path=plugin_dir, error=f"bad manifest: {exc}"
                )
                continue
            self._load_plugin(plugin_dir, manifest)

    def _load_plugin(self, path: Path, manifest: dict) -> None:
        name = str(manifest.get("name") or path.name)
        info = XpiInfo(
            name=name,
            version=str(manifest.get("version", "0.1.0")),
            path=path,
            enabled=bool(manifest.get("enabled", True)),
            platform=str(manifest.get("platform", "all")),
            entry=str(manifest.get("entry", "")),
            loaded_at=datetime.now(),
            hooks=dict(manifest.get("hooks", {})),
        )
        self.plugins[name] = info

        if not info.enabled:
            logger.log_structured("INFO", "xpi.manager", f"{name} is disabled — not loaded")
            return

        main_file = path / str(manifest.get("main", f"{name}.py"))
        if not main_file.exists():
            main_file = path / "__init__.py"
        if not main_file.exists():
            info.error = f"entry point not found (looked for {manifest.get('main', name + '.py')})"
            logger.log_structured("WARN", "xpi.manager", f"{name}: {info.error}")
            return

        try:
            module = self._import_module(name, main_file)
        except Exception as exc:  # noqa: BLE001 - one plugin must not stop the rest
            info.error = f"import failed: {type(exc).__name__}: {exc}"
            logger.log_error("xpi.manager", f"{name}: {info.error}")
            return

        self._modules[name] = module

        try:
            instance = self._instantiate(name, module, info.entry)
        except Exception as exc:  # noqa: BLE001
            info.error = f"could not instantiate: {type(exc).__name__}: {exc}"
            logger.log_error("xpi.manager", f"{name}: {info.error}")
            return

        self.instances[name] = instance
        self.registry.register(name, instance.version, instance.platform or info.platform)
        logger.log_structured("INFO", "xpi.manager", f"loaded {name} v{instance.version}")

    def _import_module(self, name: str, path: Path) -> Any:
        """Import a plugin file under a stable, unique module name."""
        qualified = f"xli_xpi_plugin_{name.replace('-', '_')}"

        # Drop any cached bytecode first. CPython validates a .pyc by source
        # mtime *in whole seconds* and size, so editing a plugin and reloading
        # within the same second — or making an equal-length edit — leaves the
        # cache looking valid and the old code runs. Hot reload has to read the
        # source it was asked to read.
        try:
            cached = importlib.util.cache_from_source(str(path))
            Path(cached).unlink(missing_ok=True)
        except (NotImplementedError, OSError):
            pass
        importlib.invalidate_caches()

        spec = importlib.util.spec_from_file_location(qualified, str(path))
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot build an import spec for {path}")
        module = importlib.util.module_from_spec(spec)
        # Register before exec so a plugin importing itself, or two plugins
        # sharing a helper name, do not clobber each other mid-import.
        sys.modules[qualified] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(qualified, None)
            raise
        return module

    @staticmethod
    def _instantiate(name: str, module: Any, entry: str) -> XpiPlugin:
        """Find and construct the plugin class.

        `entry` names the class explicitly; without it the module must expose
        exactly one XpiPlugin subclass, so a stray helper class cannot be
        picked up by accident.
        """
        if entry:
            cls = getattr(module, entry, None)
            if cls is None:
                raise AttributeError(f"{name}: no class named {entry!r}")
        else:
            candidates = [
                value
                for key, value in vars(module).items()
                if isinstance(value, type)
                and issubclass(value, XpiPlugin)
                and value is not XpiPlugin
                and value.__module__ == module.__name__
            ]
            if not candidates:
                raise AttributeError(f"{name}: no XpiPlugin subclass found")
            if len(candidates) > 1:
                names = ", ".join(sorted(c.__name__ for c in candidates))
                raise AttributeError(
                    f"{name}: multiple XpiPlugin subclasses ({names}) — set "
                    f'"entry" in manifest.json'
                )
            cls = candidates[0]

        instance = cls()
        if not isinstance(instance, XpiPlugin):
            raise TypeError(f"{name}: {cls.__name__} is not an XpiPlugin")
        return instance

    # --------------------------------------------------------------- lifecycle
    def dispatch(self, hook: str, platform: str | None = None, **context: Any) -> DispatchReport:
        """Call `hook` on every enabled plugin whose platform matches.

        Returns a report rather than raising: a plugin that throws is recorded
        and the others still run.
        """
        report = DispatchReport(hook=hook)

        for name, plugin in sorted(self.instances.items()):
            info = self.plugins.get(name)
            if info is not None and not info.enabled:
                continue
            if platform and info is not None and info.platform not in (platform, "all"):
                continue

            handler = getattr(plugin, hook, None)
            if not callable(handler):
                continue

            report.called.append(name)
            try:
                result = handler(context)
                if result is not None:
                    report.results[name] = result
            except Exception as exc:  # noqa: BLE001 - isolate the failure
                report.errors.append(
                    {"plugin": name, "error": f"{type(exc).__name__}: {exc}"}
                )
                logger.log_error("xpi.manager", f"{name}.{hook} failed", exc=exc)

        return report

    def call_hook(self, name: str, hook: str, *args: Any, **kwargs: Any) -> Any:
        """Call one hook on one plugin. Kept for compatibility."""
        plugin = self.instances.get(name)
        if plugin is None:
            return None
        handler = getattr(plugin, hook, None)
        if not callable(handler):
            return None
        try:
            return handler(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.log_error("xpi.manager", f"{name}.{hook} failed", exc=exc)
            return None

    # ----------------------------------------------------------------- control
    def get(self, name: str) -> XpiInfo | None:
        return self.plugins.get(name)

    def instance(self, name: str) -> XpiPlugin | None:
        return self.instances.get(name)

    def list_plugins(self) -> list[dict[str, Any]]:
        return [info.to_dict() for info in self.plugins.values()]

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """Enable or disable a plugin and rewrite its manifest accordingly."""
        info = self.plugins.get(name)
        if info is None:
            return False

        manifest_path = info.path / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                manifest = {}
            manifest["enabled"] = enabled
            manifest_path.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )

        info.enabled = enabled
        if enabled:
            self.reload(name)
        else:
            self._teardown(name)
        return True

    def reload(self, name: str) -> bool:
        """Re-import a plugin from disk (hot reload)."""
        info = self.plugins.get(name)
        if info is None:
            return False

        self._teardown(name)

        manifest_path = info.path / "manifest.json"
        if not manifest_path.exists():
            return False
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.log_error("xpi.manager", f"reload {name}: bad manifest", exc=exc)
            return False

        before = len(self.instances)
        self._load_plugin(info.path, manifest)
        reloaded = name in self.instances
        logger.log_structured(
            "INFO", "xpi.manager", f"reloaded {name}" if reloaded else f"reload failed: {name}"
        )
        return reloaded or len(self.instances) > before

    def _teardown(self, name: str) -> None:
        """Unload a plugin: on_unload hook, then drop every reference."""
        plugin = self.instances.pop(name, None)
        if plugin is not None:
            try:
                plugin.on_unload({})
            except Exception as exc:  # noqa: BLE001
                logger.log_error("xpi.manager", f"{name}.on_unload failed", exc=exc)

        self.registry.unregister(name)
        qualified = f"xli_xpi_plugin_{name.replace('-', '_')}"
        # pop, not del: the module may never have been registered.
        sys.modules.pop(qualified, None)
        self._modules.pop(name, None)

    def unload_all(self) -> None:
        for name in list(self.instances):
            self._teardown(name)


def get_xpi_manager(root: Path | None = None) -> XpiManager:
    """Process-wide XpiManager."""
    return XpiManager(root)
