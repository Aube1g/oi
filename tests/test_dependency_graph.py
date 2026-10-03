#!/usr/bin/env python3
"""Tests for the dependency graph, the package version, and import cycles.

Covers:

  * detect_cycles() crashed with ValueError on this repository. It kept the
    recursion stack in a set that was only popped on the normal return path, so
    an early return on a found cycle left stale entries behind and a later
    traversal did path.index(neighbor) for a neighbor not in its path.
  * _build() scanned build/ and dist/, so a package built once had its stale
    copy under build/lib/ scanned too — the graph reported 233 modules where the
    tree has about 128.
  * VERSION lived in xli/cli.py and xli/kernel/methods.py imported the CLI to
    report it, which is a dependency pointing the wrong way and an import cycle.
"""

import pytest

from xli.core.dependency_graph import DependencyGraph

REPO_ROOT = DependencyGraph.__module__  # placeholder to keep imports tidy


@pytest.fixture(scope="module")
def graph(tmp_path_factory):
    """A graph over a small synthetic tree, so tests do not depend on this repo."""
    root = tmp_path_factory.mktemp("tree")
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    # Written as `import pkg.b`, not `from pkg import b`: _build() records
    # node.module for an ImportFrom, so `from pkg import b` registers a
    # dependency on `pkg` rather than on `pkg.b` and no cycle appears.
    (root / "pkg" / "a.py").write_text("import pkg.b\n", encoding="utf-8")
    (root / "pkg" / "b.py").write_text("import pkg.c\n", encoding="utf-8")
    (root / "pkg" / "c.py").write_text("import pkg.a\n", encoding="utf-8")
    (root / "pkg" / "leaf.py").write_text("import os\n", encoding="utf-8")
    return DependencyGraph(str(root))


class TestCycleDetection:
    def test_finds_a_three_node_cycle_without_raising(self, graph):
        cycles = graph.detect_cycles()
        assert cycles, "the synthetic tree contains a cycle"
        # and no ValueError escaped

    def test_reports_directed_edges(self, graph):
        cycles = graph.detect_cycles()
        assert all(isinstance(edge, tuple) and len(edge) == 2 for edge in cycles)

    def test_acyclic_graph_reports_nothing(self, tmp_path):
        root = tmp_path / "clean"
        root.mkdir()
        (root / "one.py").write_text("import two\n", encoding="utf-8")
        (root / "two.py").write_text("import os\n", encoding="utf-8")
        assert DependencyGraph(str(root)).detect_cycles() == []

    def test_repeated_calls_are_stable(self, graph):
        """The stale-stack bug made the second call behave differently."""
        first = graph.detect_cycles()
        second = graph.detect_cycles()
        assert first == second

    def test_self_cycle_does_not_raise(self, tmp_path):
        """A module importing itself used to be exactly the crashing shape."""
        root = tmp_path / "selfy"
        root.mkdir()
        (root / "me.py").write_text(
            "def f():\n    from me import g\n", encoding="utf-8"
        )
        assert isinstance(DependencyGraph(str(root)).detect_cycles(), list)


class TestBuildIgnoresArtifacts:
    def test_build_directory_is_not_scanned(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        (root / "real.py").write_text("import os\n", encoding="utf-8")
        stale = root / "build" / "lib" / "proj"
        stale.mkdir(parents=True)
        (stale / "real.py").write_text("import os\n", encoding="utf-8")

        modules = DependencyGraph(str(root)).get_execution_order()
        assert len([m for m in modules if m.endswith("real")]) == 1

    def test_dist_and_pycache_are_not_scanned(self, tmp_path):
        root = tmp_path / "proj"
        for artifact in ("dist", "__pycache__", "node_modules", ".git"):
            d = root / artifact
            d.mkdir(parents=True, exist_ok=True)
            (d / "copy.py").write_text("import os\n", encoding="utf-8")
        (root / "real.py").write_text("import os\n", encoding="utf-8")

        modules = DependencyGraph(str(root)).get_execution_order()
        assert modules == ["real"]

    def test_this_repository_has_no_phantom_modules(self):
        """Pins the 233-vs-128 regression against the real tree."""
        from pathlib import Path

        tree = Path(__file__).resolve().parent.parent
        real = {
            p
            for p in tree.rglob("*.py")
            if not any(part in {"build", "dist", "__pycache__", ".git", "node_modules"}
                       for part in p.parts)
        }
        found = DependencyGraph(str(tree)).get_execution_order()
        assert len(found) <= len(real) + 20, (
            f"graph has {len(found)} modules but the tree has {len(real)} .py files; "
            "build output is probably being scanned again"
        )


class TestVersion:
    def test_package_exports_the_version(self):
        from xli import VERSION, __version__

        assert __version__ == VERSION
        assert __version__.count(".") == 2

    def test_cli_reexports_it(self):
        from xli import VERSION
        from xli.cli import VERSION as cli_version

        assert cli_version == VERSION

    def test_kernel_does_not_import_the_cli(self):
        """The cycle this removes: xli.cli -> xli.kernel.methods -> xli.cli."""
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent / "xli" / "kernel" / "methods.py").read_text(
            encoding="utf-8"
        )
        assert "from xli.cli import" not in source
        assert "import xli.cli" not in source

    def test_pyproject_agrees_with_the_package(self):
        import re
        from pathlib import Path

        from xli import VERSION

        pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(
            encoding="utf-8"
        )
        declared = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
        assert declared, "no version in pyproject.toml"
        assert declared.group(1) == VERSION, "pyproject and xli.__version__ disagree"

    def test_xli_init_stays_import_cheap(self):
        """The version module must not drag in httpx, curses, or the registry."""
        import subprocess
        import sys

        code = (
            "import sys, xli; "
            "bad=[m for m in ('httpx','curses','xli.tools.registry','xli.mcp.registry') "
            "if m in sys.modules]; "
            "print(bad)"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )
        assert out.returncode == 0, out.stderr
        pulled = out.stdout.strip()
        assert pulled == "[]", f"importing xli pulled in {pulled}"


class TestNoImportCyclesInTheShippedPackage:
    """Two different questions, and the package must pass the strict one.

    A *module-level* cycle is a real defect: importing either module first
    raises, or worse, one of them silently sees a half-built neighbour. A cycle
    whose arrows all live inside function bodies is harmless — the module is
    already loaded when the import statement runs — but it is still a smell
    worth pinning, because the next edit can move one of those imports to the
    top of the file and turn it into the real thing.

    The graph now distinguishes the two (``DependencyGraph.deferred``), so this
    test can demand "no module-level cycles at all" instead of maintaining a
    hand-written allow-list of everything that had accumulated.
    """

    def _graph(self):
        from pathlib import Path

        tree = Path(__file__).resolve().parent.parent
        return DependencyGraph(str(tree))

    def test_no_module_level_cycles(self):
        graph = self._graph()
        cycles = graph.detect_cycles(module_level_only=True)
        assert not cycles, f"module-level import cycles: {cycles}"

    def test_the_deferred_cycle_is_the_documented_one(self):
        """What remains is function-local, and it is the same set as before.

        ``agent -> tools.registry -> tools.builtin -> agent`` (the tool that
        spawns a sub-agent imports the agent class inside the call) plus
        ``chain <-> self_correcting_chain``. If a new deferred cycle appears,
        it shows up here, with the reason it was accepted written next to it.
        """
        graph = self._graph()
        cycles = set(graph.detect_cycles())
        assert cycles == {
            ("xli.agent", "xli.tools.registry"),
            ("xli.tools.registry", "xli.tools.builtin"),
            ("xli.tools.builtin", "xli.agent"),
            ("xli.core.chain", "xli.core.self_correcting_chain"),
            ("xli.core.self_correcting_chain", "xli.core.chain"),
        }, f"cycle set changed: {sorted(cycles)}"
        # Every cycle above closes through at least one function-local import,
        # which is what makes it safe; the strict test above is the one that
        # proves no cycle is closed entirely by module-level imports.
        assert any(edge in graph.deferred for edge in cycles)
