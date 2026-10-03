#!/usr/bin/env python3
"""
XPI Plugin Manager v4 — загрузка, lifecycle, hot-reload
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any
from dataclasses import dataclass, field
from datetime import datetime

from xli.paths import xli_path
from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.xpi.manager")

XPI_DIR = xli_path("xpi")


@dataclass
class XpiInfo:
    """Plugin metadata"""
    name: str
    version: str
    path: Path
    enabled: bool = True
    loaded_at: datetime | None = None
    hooks: dict[str, Any] = field(default_factory=dict)


class XpiManager:
    """Manages XPI plugins with hot-reload"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        XPI_DIR.mkdir(parents=True, exist_ok=True)
        self.plugins: dict[str, XpiInfo] = {}
        self._modules: dict[str, Any] = {}
        self._load_all()

        logger.log_structured("INFO", "xpi.manager",
                             f"Loaded {len(self.plugins)} plugins")

    def _load_all(self):
        """Scan and load all plugins"""
        for plugin_dir in XPI_DIR.iterdir():
            if not plugin_dir.is_dir():
                continue

            manifest = plugin_dir / "manifest.json"
            if manifest.exists():
                try:
                    import json
                    with open(manifest) as f:
                        info = json.load(f)
                    self._load_plugin(plugin_dir, info)
                except Exception as e:
                    logger.log_error("xpi.manager", f"Failed to load {plugin_dir.name}", exc=e)

    def _load_plugin(self, path: Path, manifest: dict):
        """Load single plugin"""
        name = manifest.get("name", path.name)

        # Find main module
        main_file = path / manifest.get("main", "__init__.py")
        if not main_file.exists():
            main_file = path / f"{name}.py"

        if main_file.exists():
            spec = importlib.util.spec_from_file_location(f"xli.xpi.{name}", main_file)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            self._modules[name] = module

        self.plugins[name] = XpiInfo(
            name=name,
            version=manifest.get("version", "0.1.0"),
            path=path,
            enabled=manifest.get("enabled", True),
            loaded_at=datetime.now(),
            hooks=manifest.get("hooks", {})
        )

        logger.log_structured("INFO", "xpi.manager", f"Plugin loaded: {name}")

    def get(self, name: str) -> XpiInfo | None:
        """Get plugin info"""
        return self.plugins.get(name)

    def list_plugins(self) -> list[dict]:
        """List all plugins"""
        return [
            {
                "name": p.name,
                "version": p.version,
                "enabled": p.enabled,
                "path": str(p.path)
            }
            for p in self.plugins.values()
        ]

    def reload(self, name: str) -> bool:
        """Hot-reload plugin"""
        if name not in self.plugins:
            return False

        plugin = self.plugins[name]
        manifest_path = plugin.path / "manifest.json"

        if manifest_path.exists():
            try:
                import json
                with open(manifest_path) as f:
                    manifest = json.load(f)

                # Remove old module
                if name in self._modules:
                    del sys.modules[f"xli.xpi.{name}"]

                self._load_plugin(plugin.path, manifest)
                logger.log_structured("INFO", "xpi.manager", f"Reloaded: {name}")
                return True
            except Exception as e:
                logger.log_error("xpi.manager", f"Reload failed: {name}", exc=e)

        return False

    def call_hook(self, name: str, hook: str, *args, **kwargs) -> Any:
        """Call plugin hook"""
        if name not in self._modules:
            return None

        module = self._modules[name]
        hook_func = getattr(module, hook, None)

        if hook_func and callable(hook_func):
            try:
                return hook_func(*args, **kwargs)
            except Exception as e:
                logger.log_error("xpi.manager", f"Hook {hook} failed in {name}", exc=e)

        return None


def get_xpi_manager() -> XpiManager:
    """Get singleton XpiManager"""
    return XpiManager()
