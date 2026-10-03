#!/usr/bin/env python3
"""
XLI Dependency Graph v4 — Import graph, execution order, breaking changes
"""

import ast
from pathlib import Path
from collections import defaultdict, deque

from xli.core.logger import StructuredLogger

#: Directory names that hold generated or third-party code. Without build/ and
#: dist/ in here, a package that has been built once gets scanned twice — the
#: stale copy under build/lib/ doubled the graph and reported phantom modules.
_IGNORED_PARTS = frozenset({
    "venv", ".venv", "env", "__pycache__", ".git", "node_modules",
    "build", "dist", "out", "target", ".tox", ".nox", "site-packages",
    ".mypy_cache", ".ruff_cache", ".pytest_cache", "egg-info",
})

logger = StructuredLogger("xli.deps")


class DependencyGraph:
    """Python import dependency graph"""

    def __init__(self, directory: str = "."):
        self.directory = Path(directory)
        self.graph: dict[str, set[str]] = defaultdict(set)
        self.reverse_graph: dict[str, set[str]] = defaultdict(set)
        self.file_modules: dict[str, str] = {}
        #: Edges that exist only inside a function body. They cannot create a
        #: cycle at import time — the module is already loaded by the time the
        #: import statement runs — so they are tracked separately instead of
        #: being reported as if they were real.
        self.deferred: set[tuple[str, str]] = set()
        self._build()

    def _build(self):
        """Build import graph"""
        for py_file in self.directory.rglob("*.py"):
            if any(part in _IGNORED_PARTS for part in py_file.parts):
                continue

            module_name = self._get_module_name(py_file)
            self.file_modules[str(py_file)] = module_name

            try:
                with open(py_file, encoding="utf-8") as f:
                    tree = ast.parse(f.read())

                # Walk the module top level and each function body separately:
                # a name imported inside a function is resolved after the module
                # is fully loaded, so it cannot participate in an import cycle.
                deferred_nodes: set[int] = set()
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        for child in ast.walk(node):
                            deferred_nodes.add(id(child))

                for node in ast.walk(tree):
                    targets: list[str] = []
                    if isinstance(node, ast.Import):
                        targets = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        targets = [node.module]
                    for target in targets:
                        self.graph[module_name].add(target)
                        if id(node) in deferred_nodes:
                            self.deferred.add((module_name, target))

            except Exception as e:
                logger.log_error("deps", f"Parse failed: {py_file}", exc=e)

        # Build reverse graph
        for module, deps in self.graph.items():
            for dep in deps:
                self.reverse_graph[dep].add(module)

        logger.log_structured("INFO", "deps",
                             f"Graph built: {len(self.graph)} modules")

    def _get_module_name(self, path: Path) -> str:
        """Convert path to module name"""
        rel = path.relative_to(self.directory)
        return str(rel.with_suffix("")).replace("/", ".").replace("\\", ".")

    def get_execution_order(self) -> list[str]:
        """Topological sort for execution order"""
        in_degree = dict.fromkeys(self.graph, 0)
        for deps in self.graph.values():
            for dep in deps:
                if dep in in_degree:
                    in_degree[dep] += 1

        queue = deque([m for m, d in in_degree.items() if d == 0])
        order = []

        while queue:
            module = queue.popleft()
            order.append(module)

            for dependent in self.reverse_graph.get(module, []):
                if dependent in in_degree:
                    in_degree[dependent] -= 1
                    if in_degree[dependent] == 0:
                        queue.append(dependent)

        if len(order) != len(in_degree):
            # Cycle detected
            remaining = set(in_degree.keys()) - set(order)
            logger.log_structured("WARN", "deps",
                                 f"Cycle detected in: {remaining}")
            order.extend(sorted(remaining))

        return order

    def detect_cycles(self, *, module_level_only: bool = False) -> list[tuple[str, str]]:
        """Find circular dependencies, as (module, module) edges.

        The previous version kept the recursion stack in a set that was only
        popped on the normal return path. Returning early on a found cycle left
        stale entries behind, so a later top-level traversal could see a
        neighbour "on the stack" that was not in its own path — and
        `path.index(neighbour)` then raised ValueError. Reproducible on this
        repository. The set is now maintained in a finally block, so it can
        never disagree with `path`.
        """
        cycles: list[tuple[str, str]] = []
        visited: set[str] = set()
        on_path: set[str] = set()
        deferred = self.deferred if module_level_only else set()

        def neighbours(node: str) -> list[str]:
            return sorted(
                target
                for target in self.graph.get(node, ())
                if (node, target) not in deferred
            )

        def dfs(node: str, path: list[str]) -> list[str] | None:
            visited.add(node)
            on_path.add(node)
            try:
                for neighbor in neighbours(node):
                    if neighbor in on_path:
                        # neighbour is on the current path: a genuine cycle
                        start = path.index(neighbor)
                        return path[start:] + [neighbor]
                    if neighbor not in visited:
                        found = dfs(neighbor, path + [neighbor])
                        if found:
                            return found
                return None
            finally:
                on_path.discard(node)

        for module in sorted(self.graph):
            if module in visited:
                continue
            cycle = dfs(module, [module])
            if cycle:
                for i in range(len(cycle) - 1):
                    edge = (cycle[i], cycle[i + 1])
                    if edge not in cycles:
                        cycles.append(edge)

        logger.log_structured("INFO", "deps",
                             f"Found {len(cycles)} cycle edges")
        return cycles

    def get_affected_files(self, changed_file: str) -> list[str]:
        """Get files affected by change"""
        module = self._get_module_name(Path(changed_file))

        affected = set()
        queue = deque([module])

        while queue:
            current = queue.popleft()
            for dependent in self.reverse_graph.get(current, []):
                if dependent not in affected:
                    affected.add(dependent)
                    queue.append(dependent)

        # Convert back to files
        result = []
        for file_path, mod in self.file_modules.items():
            if mod in affected:
                result.append(file_path)

        logger.log_structured("DEBUG", "deps",
                             f"Change in {changed_file} affects {len(result)} files")
        return result

    def get_dependencies(self, module: str) -> set[str]:
        """Get direct dependencies of module"""
        return self.graph.get(module, set())

    def get_dependents(self, module: str) -> set[str]:
        """Get modules that depend on this"""
        return self.reverse_graph.get(module, set())


def get_dependency_graph(directory: str = ".") -> DependencyGraph:
    """Get DependencyGraph instance"""
    return DependencyGraph(directory)

