#!/usr/bin/env python3
"""The sub-agent registry.

Specs live as one JSON file each, so they can be read, diffed and hand-edited
without a database, and a project can ship its own under `.xli/agents/`. Lookup
order is project, then user, then built-ins, which means a project can override
a built-in by name without deleting anything.

Loading never raises. A malformed file is reported as a problem against that
name and skipped, because one bad hand-edit should not take the whole registry
down — and certainly should not stop `xli run`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from xli.agents.builtin import BUILTIN_SPECS
from xli.agents.spec import AgentSpec
from xli.paths import xli_path

#: File name a spec is stored under, given its name.
FILENAME = "{name}.json"


class AgentRegistry:
    """Loads, saves and validates sub-agent specs."""

    def __init__(self, *, project_root: Path | None = None):
        self.project_root = Path(project_root) if project_root else None
        self._cache: dict[str, AgentSpec] | None = None
        self._problems: dict[str, str] = {}

    # ------------------------------------------------------------------- paths
    @property
    def user_dir(self) -> Path:
        return xli_path("agents")

    @property
    def project_dir(self) -> Path | None:
        return self.project_root / ".xli" / "agents" if self.project_root else None

    def search_dirs(self) -> list[Path]:
        """Directories searched, highest precedence first."""
        dirs: list[Path] = []
        if self.project_dir:
            dirs.append(self.project_dir)
        dirs.append(self.user_dir)
        return dirs

    # ------------------------------------------------------------------ loading
    def load(self, *, force: bool = False) -> dict[str, AgentSpec]:
        """All specs by name. Cached; pass force=True to re-read from disk."""
        if self._cache is not None and not force:
            return self._cache

        specs: dict[str, AgentSpec] = {}
        self._problems = {}

        # Built-ins first, so a user or project file with the same name wins.
        for spec in BUILTIN_SPECS:
            spec.builtin = True
            specs[spec.name] = spec

        # Lowest precedence last-written wins, so walk user then project.
        for directory in reversed(self.search_dirs()):
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.json")):
                spec, problem = _read_spec(path)
                if problem:
                    self._problems[path.stem] = problem
                    continue
                spec.builtin = False
                specs[spec.name] = spec

        self._cache = specs
        return specs

    def problems(self) -> dict[str, str]:
        """Load-time failures, keyed by the name we tried to read."""
        self.load()
        return dict(self._problems)

    # ------------------------------------------------------------------ queries
    def names(self) -> list[str]:
        return sorted(self.load())

    def get(self, name: str) -> AgentSpec | None:
        return self.load().get(name)

    def __contains__(self, name: object) -> bool:
        return name in self.load()

    def __len__(self) -> int:
        return len(self.load())

    def all(self) -> list[AgentSpec]:
        return [self.load()[name] for name in self.names()]

    def builtins(self) -> list[AgentSpec]:
        return [spec for spec in self.all() if spec.builtin]

    def custom(self) -> list[AgentSpec]:
        return [spec for spec in self.all() if not spec.builtin]

    # ----------------------------------------------------------------- mutating
    def path_for(self, name: str, *, project: bool = False) -> Path:
        directory = (self.project_dir if project else None) or self.user_dir
        return directory / FILENAME.format(name=name)

    def save(self, spec: AgentSpec, *, project: bool = False) -> Path:
        """Write a spec. Refuses to overwrite a built-in by accident."""
        if spec.builtin and not project:
            raise ValueError(
                f"{spec.name!r} is a built-in; save it into the project "
                "(`--project`) to override it rather than editing the user copy"
            )
        path = self.path_for(spec.name, project=project)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(spec.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        self._cache = None
        return path

    def delete(self, name: str) -> bool:
        """Remove a user or project spec. Built-ins cannot be deleted."""
        spec = self.get(name)
        if spec is None or spec.builtin:
            return False
        removed = False
        for directory in self.search_dirs():
            path = directory / FILENAME.format(name=name)
            if path.is_file():
                path.unlink()
                removed = True
        self._cache = None
        return removed

    # -------------------------------------------------------------- validation
    def verify(self, available_tools: list[str] | None = None) -> dict[str, list[str]]:
        """Check every spec, returning only the ones with problems."""
        report: dict[str, list[str]] = {}
        for name, problem in self.problems().items():
            report[name] = [problem]
        for spec in self.all():
            problems = spec.validate(available_tools)
            if problems:
                report[spec.name] = problems
        return report

    def summary(self) -> list[dict[str, Any]]:
        """One row per spec, shaped for `--json` output."""
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "tools": list(spec.tools),
                "mode": spec.mode,
                "max_steps": spec.max_steps,
                "builtin": spec.builtin,
                "tags": list(spec.tags),
            }
            for spec in self.all()
        ]


def _read_spec(path: Path) -> tuple[AgentSpec | None, str]:
    """Read one spec file, returning (spec, "") or (None, reason)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"cannot read {path.name}: {exc}"
    if not isinstance(data, dict):
        return None, f"{path.name} must contain a JSON object"
    try:
        spec = AgentSpec.from_dict(data)
    except (TypeError, ValueError) as exc:
        return None, f"{path.name}: {exc}"
    if spec.name != path.stem:
        return None, f"{path.name} declares name {spec.name!r}, expected {path.stem!r}"
    return spec, ""


_REGISTRY: AgentRegistry | None = None


def get_registry(project_root: Path | None = None, *, force: bool = False) -> AgentRegistry:
    """Shared registry.

    `project_root` is part of the identity: a registry built for one project
    must not be handed to another, or the second would silently see the first
    project's overrides. Asking for a different root builds a fresh one.
    """
    global _REGISTRY
    wanted = Path(project_root) if project_root else None
    if force or _REGISTRY is None or _REGISTRY.project_root != wanted:
        _REGISTRY = AgentRegistry(project_root=wanted)
    return _REGISTRY


def reset_registry() -> None:
    """Drop the shared registry. For tests."""
    global _REGISTRY
    _REGISTRY = None
