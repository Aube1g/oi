#!/usr/bin/env python3
"""XLI dependency view — the import graph, turned into something readable.

The question this module answers is not "draw me a graph". It is the question
you actually have while editing: **if I touch this file, what else moves?**
And its inverse: **which module can I use without dragging half the project
in?** The old `xli graph <module>` printed a tree of import names — pretty,
and useless, because the direction that matters (who depends on me) was
missing, and every leaf looked the same.

So the model here is built the other way round:

* ``GraphModel.importers`` — for every module, who imports it.
* ``GraphModel.impact(path)`` — direct importers, the transitive blast radius,
  the module's own dependencies, and the test files that mention it.
* ``GraphModel.hot`` — the modules everything leans on (touching one is a
  review, not an edit).
* ``GraphModel.entry_points`` — nothing imports them: CLI entry points, plugin
  roots, `__main__`. Safe to read, they belong to nobody.
* ``GraphModel.cycles`` — only import-time cycles count. Imports inside a
  function body are noted and set aside; they cannot deadlock an import.

Rendering is separated from the model so the CLI, the TUI panel and the
``dep_graph`` tool all show the same numbers in their own clothes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from xli.core.dependency_graph import DependencyGraph

#: Style names shared with the rest of the UI (see `xli.tui.palette`).
NORMAL = "normal"
DIM = "dim"
BOLD = "bold"
ACCENT = "accent"
GOOD = "good"
WARN = "warn"
BAD = "bad"
CODE = "code"

Span = tuple[str, str]
Row = list[Span]


def _module_of(path: Path, root: Path) -> str:
    """`src/xli/ui/graph.py` -> `xli.ui.graph`, `xli/ui/__init__.py` -> `xli.ui`."""
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        relative = path
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


@dataclass(frozen=True)
class ModuleInfo:
    name: str
    path: str
    deps: tuple[str, ...]
    importers: tuple[str, ...]

    @property
    def is_leaf(self) -> bool:
        return not self.deps

    @property
    def is_entry(self) -> bool:
        return not self.importers


@dataclass
class Impact:
    """What moves when `target` is touched. The answer to "can I break this?"."""

    target: str
    module: str = ""
    direct: list[str] = field(default_factory=list)
    transitive: list[str] = field(default_factory=list)
    deps: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    missing: bool = False

    @property
    def risky(self) -> bool:
        return len(self.direct) >= 3


@dataclass
class GraphModel:
    root: Path
    modules: dict[str, ModuleInfo]
    cycles: list[tuple[str, str]]
    deferred_edges: int = 0
    file_count: int = 0

    # ------------------------------------------------------------- questions
    def search(self, needle: str) -> list[str]:
        needle = needle.strip().lower()
        if not needle:
            return []
        matches = [name for name in self.modules if needle in name.lower()]
        return sorted(matches, key=lambda name: (len(name), name))

    def resolve(self, target: str) -> str | None:
        """A path, a module name or a bare filename -> the module name."""
        text = target.strip().replace("\\", "/")
        if not text:
            return None
        text = text.removesuffix(".py").replace("/", ".")
        if text.endswith(".__init__"):
            text = text[: -len(".__init__")]
        if text in self.modules:
            return text
        matches = self.search(text)
        if matches:
            return matches[0]
        stem = text.rsplit(".", 1)[-1]
        matches = self.search(stem)
        return matches[0] if matches else None

    def hot(self, limit: int = 10) -> list[tuple[str, int]]:
        ranked = [
            (name, len(info.importers))
            for name, info in self.modules.items()
            if info.importers
        ]
        ranked.sort(key=lambda pair: (-pair[1], pair[0]))
        return ranked[:limit]

    def entry_points(self) -> list[str]:
        """Modules nothing imports — CLI roots, plugins, `__main__`."""
        return sorted(
            name for name, info in self.modules.items() if info.is_entry and not name.endswith("__init__")
        )

    def leaves(self) -> list[str]:
        return sorted(name for name, info in self.modules.items() if info.is_leaf)

    def package_size(self) -> list[tuple[str, int]]:
        """Modules per top-level package: `xli.ui` -> 9."""
        counts: dict[str, int] = {}
        for name in self.modules:
            parts = name.split(".")
            key = ".".join(parts[:2]) if len(parts) > 1 else parts[0]
            counts[key] = counts.get(key, 0) + 1
        ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
        return ranked

    def impact(self, target: str) -> Impact:
        module = self.resolve(target)
        if module is None:
            return Impact(target=target, missing=True)

        info = self.modules[module]
        direct = sorted(info.importers)

        # Everything that transitively ends up importing `module`. BFS over
        # the reverse edges: the first time a module is reached is the shortest
        # chain from it to the target, which is the number worth showing.
        seen: dict[str, int] = {}
        frontier = [(name, 1) for name in direct]
        while frontier:
            name, depth = frontier.pop(0)
            if name in seen or name == module:
                continue
            seen[name] = depth
            for parent in self.modules.get(name, ModuleInfo(name, "", (), ())).importers:
                if parent not in seen:
                    frontier.append((parent, depth + 1))

        tests = [
            info.path
            for name, info in self.modules.items()
            if name.startswith("tests") or "/tests/" in info.path or info.path.startswith("tests/")
        ]
        hit_tests = sorted(
            path for path in tests if _test_mentions(Path(self.root) / path, module)
        )

        return Impact(
            target=target,
            module=module,
            direct=direct,
            # Direct importers are already listed; the chain is what is left.
            transitive=[
                name
                for name, _ in sorted(seen.items(), key=lambda pair: pair[1])
                if name not in direct
            ],
            deps=sorted(info.deps),
            tests=hit_tests,
        )


def _test_mentions(path: Path, module: str) -> bool:
    """Does this test file refer to the module at all?

    Cheap substring check over imports and `monkeypatch` targets rather than an
    AST walk: a test that names the module anywhere is a test that has an
    opinion about it.
    """
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    stem = module.rsplit(".", 1)[-1]
    if module in text:
        return True
    return bool(re.search(rf"\b{re.escape(stem)}\b", text)) and "import" in text


def build(root: str | Path = ".", *, prefix: str = "") -> GraphModel:
    """Scan `root` and return the model. `prefix` narrows to one package."""
    directory = Path(root)
    graph = DependencyGraph(str(directory))

    internal = set(graph.graph)
    modules: dict[str, ModuleInfo] = {}
    for module, deps in graph.graph.items():
        if prefix and not module.startswith(prefix):
            continue
        info_path = next(
            (path for path, name in graph.file_modules.items() if name == module), ""
        )
        modules[module] = ModuleInfo(
            name=module,
            path=str(Path(info_path).resolve().relative_to(directory.resolve()))
            if info_path
            else module.replace(".", "/") + ".py",
            deps=tuple(
                sorted(dep for dep in deps if dep in internal or dep.startswith(prefix))
            ),
            importers=(),
        )
    # Importers are computed from the (filtered) module set, so the CLI and the
    # TUI agree about who matters when a package is in focus.
    for module, info in modules.items():
        importers = tuple(sorted(graph.reverse_graph.get(module, ())))
        modules[module] = ModuleInfo(info.name, info.path, info.deps, importers)

    cycles = [
        edge
        for edge in graph.detect_cycles(module_level_only=True)
        if (not prefix or edge[0].startswith(prefix))
    ]
    deferred = len(graph.deferred)
    return GraphModel(
        root=directory,
        modules=modules,
        cycles=cycles,
        deferred_edges=deferred,
        file_count=len(graph.file_modules),
    )


# ------------------------------------------------------------------ rendering
def _human_depths(impact: Impact, limit: int = 6) -> list[str]:
    return impact.transitive[:limit]


def panel_rows(
    model: GraphModel,
    *,
    width: int = 60,
    limit: int = 8,
    focus: str = "",
) -> list[Row]:
    """Styled rows for the graph panel — shared by the TUI and the CLI.

    Read it top-down: what everything leans on, what nothing points at, and
    whether anything is tangled. Bars, not numbers, for the hub sizes: the
    relative weight is the useful part.
    """
    rows: list[Row] = []
    hot = model.hot(limit)
    top = max((count for _, count in hot), default=1) or 1

    rows.append([("модулей ", DIM), (str(len(model.modules)), BOLD), ("  связей ", DIM), (str(sum(len(m.deps) for m in model.modules.values())), BOLD)])
    if model.cycles:
        rows.append([("циклы импорта: ", DIM), (str(len(model.cycles)), BAD)])
    else:
        rows.append([("циклы импорта: ", DIM), ("нет", GOOD)])
    rows.append([("", DIM)])

    rows.append([("на них держится проект", DIM)])
    for name, count in hot:
        bar_width = max(1, int(count / top * 12))
        label = name if len(name) <= max(12, width - 26) else "…" + name[-(width - 27):]
        rows.append(
            [
                (" " * 2, DIM),
                (f"{label:<{max(12, width - 26)}}", NORMAL),
                ("█" * bar_width, ACCENT),
                (f" {count}", DIM),
            ]
        )

    depth = max(1, int(limit / 2))
    if focus:
        impact = model.impact(focus)
        rows.append([("", DIM)])
        if impact.missing:
            rows.append([("не найдено: ", WARN), (focus, NORMAL)])
        else:
            rows.append([(impact.module, ACCENT), ("  ← ", DIM), (f"{len(impact.direct)} зависят", NORMAL)])
            for name in _human_depths(impact, limit=depth):
                rows.append([(" " * 2, DIM), ("← ", DIM), (name, NORMAL)])
    return rows


def overview_text(model: GraphModel, *, width: int = 78, limit: int = 10) -> list[str]:
    """Plain-text overview for the CLI (style is applied by the caller)."""
    out: list[str] = []
    total_edges = sum(len(info.deps) for info in model.modules.values())
    out.append(
        f"модулей {len(model.modules)} · связей {total_edges} · файлов {model.file_count} · "
        f"циклов {len(model.cycles)} · отложенных импортов {model.deferred_edges}"
    )
    out.append("")
    out.append("на них держится проект")
    hot = model.hot(limit)
    top = max((count for _, count in hot), default=1) or 1
    for name, count in hot:
        bar = "█" * max(1, int(count / top * 16))
        out.append(f"  {name:<{max(16, width - 26)}} {bar} {count}")
    entries = model.entry_points()
    if entries:
        out.append("")
        out.append("точки входа (никто не импортирует)")
        for name in entries[:limit]:
            out.append(f"  {name}")
    if model.cycles:
        out.append("")
        out.append("циклы импорта (модульный уровень — настоящие)")
        for source, target in model.cycles[:limit]:
            out.append(f"  {source} → {target}")
    return out


def tree_text(
    model: GraphModel,
    start: str,
    *,
    width: int = 78,
    depth: int = 2,
    reverse: bool = False,
) -> list[str]:
    """`start` and what it pulls in (or, with `reverse`, who pulls it in)."""
    module = model.resolve(start)
    if module is None:
        return [f"не найдено: {start}"]
    info = model.modules[module]
    out = [f"{module}  ({info.path})"]
    seen: set[str] = {module}

    def walk(name: str, level: int, prefix: str) -> None:
        if level > depth:
            return
        children = model.modules.get(name)
        if children is None:
            return
        names = [item for item in (children.importers if reverse else children.deps) if item in model.modules]
        for index, child in enumerate(names):
            last = index == len(names) - 1
            connector = "└─ " if last else "├─ "
            mark = " ⟳" if (name, child) in model.cycles or (child, name) in model.cycles else ""
            if child in seen:
                out.append(f"{prefix}{connector}{child}{mark} ·")
                continue
            seen.add(child)
            out.append(f"{prefix}{connector}{child}{mark}")
            walk(child, level + 1, prefix + ("   " if last else "│  "))

    walk(module, 1, "")
    if len(out) == 1:
        out.append("  " + ("никто не импортирует" if reverse else "нет внутренних зависимостей"))
    external = sorted(dep for dep in info.deps if dep not in model.modules)
    if external and not reverse:
        out.append(f"  внешние: {', '.join(external[:8])}" + (" …" if len(external) > 8 else ""))
    return out


def impact_text(model: GraphModel, target: str, *, width: int = 78, limit: int = 12) -> list[str]:
    impact = model.impact(target)
    if impact.missing:
        return [f"не найдено: {target}"]

    out = [f"{impact.module}  ({model.modules[impact.module].path})", ""]
    if not impact.direct:
        out.append("никто не импортирует — можно менять свободно")
    else:
        out.append(f"импортируют напрямую ({len(impact.direct)})")
        for name in impact.direct[:limit]:
            out.append(f"  ← {name}")
        extra = len(impact.direct) - limit
        if extra > 0:
            out.append(f"  … и ещё {extra}")
    if impact.transitive:
        out.append("")
        out.append(f"заденет по цепочке ({len(impact.transitive)})")
        for name in impact.transitive[:limit]:
            out.append(f"  ← {name}")
    if impact.deps:
        out.append("")
        out.append(f"сам зависит от ({len(impact.deps)})")
        for name in impact.deps[:limit]:
            out.append(f"  → {name}")
    if impact.tests:
        out.append("")
        out.append(f"тесты, которые его упоминают ({len(impact.tests)})")
        for path in impact.tests[:limit]:
            out.append(f"  · {path}")
    return out


__all__ = [
    "GraphModel",
    "Impact",
    "ModuleInfo",
    "build",
    "impact_text",
    "overview_text",
    "panel_rows",
    "tree_text",
]
