#!/usr/bin/env python3
"""Optional-dependency handling.

These pin the fixes for two ways the tree broke when an optional extra was not
installed:

  * xli/core/vector_store.py guarded `faiss` but imported numpy unguarded, so
    the module raised ModuleNotFoundError at import time and the graceful
    degradation behind HAS_FAISS was unreachable.
  * xli/core/prompt_lab.py annotated a parameter `callable | None`. Lowercase
    `callable` is the builtin function, not a type, so `callable | None` raised
    TypeError while the class body was being evaluated — the module could not
    be imported at all.
"""

import importlib

import pytest


class TestVectorStoreDegrades:
    def test_module_imports_without_numpy_or_faiss(self):
        """The whole point: importing must not require the extra."""
        module = importlib.import_module("xli.core.vector_store")
        assert hasattr(module, "HAS_NUMPY")
        assert hasattr(module, "HAS_FAISS")

    def test_instantiates_without_the_extra(self):
        from xli.core.vector_store import CodeVectorStore

        store = CodeVectorStore()
        assert store is not None

    def test_search_returns_empty_instead_of_raising(self):
        from xli.core.vector_store import CodeVectorStore, HAS_FAISS, HAS_NUMPY

        store = CodeVectorStore()
        result = store.search("anything")
        if not (HAS_FAISS and HAS_NUMPY):
            assert result == []

    def test_index_is_a_noop_without_the_extra(self):
        from xli.core.vector_store import CodeVectorStore, HAS_FAISS, HAS_NUMPY

        store = CodeVectorStore()
        if not (HAS_FAISS and HAS_NUMPY):
            # Must not raise; indexing simply has nothing to write into.
            store.add_document("nonexistent.py", "def f(): pass")

    def test_no_unguarded_numpy_import(self):
        """Regression guard: numpy must sit inside a try block."""
        import ast
        from pathlib import Path

        source = Path("xli/core/vector_store.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        for node in tree.body:  # module level only
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
                assert "numpy" not in names, "numpy must be imported inside a try"

    def test_faiss_and_numpy_guarded_together(self):
        import ast
        from pathlib import Path

        tree = ast.parse(Path("xli/core/vector_store.py").read_text(encoding="utf-8"))
        guarded = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                for stmt in node.body:
                    if isinstance(stmt, ast.Import):
                        guarded.update(a.name.split(".")[0] for a in stmt.names)
        assert "numpy" in guarded
        assert "faiss" in guarded


class TestPromptLabImports:
    def test_module_imports(self):
        """`callable | None` used to raise TypeError during class creation."""
        module = importlib.import_module("xli.core.prompt_lab")
        assert hasattr(module, "PromptLab")

    def test_no_bare_callable_annotation_anywhere(self):
        """`callable` is the builtin; `Callable` is the type."""
        import ast
        from pathlib import Path

        offenders = []
        for path in Path("xli").rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                annotation = getattr(node, "annotation", None) or getattr(node, "returns", None)
                if annotation is None:
                    continue
                for sub in ast.walk(annotation):
                    if isinstance(sub, ast.Name) and sub.id == "callable":
                        offenders.append(f"{path}:{sub.lineno}")
        assert not offenders, f"lowercase 'callable' used as a type: {offenders}"


class TestOptionalDependencyDeclarations:
    @pytest.fixture
    def project(self):
        import tomllib
        from pathlib import Path

        return tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]

    def test_required_deps_are_actually_imported(self, project):
        """Nothing in the default install path should be dead weight."""
        import ast
        from pathlib import Path

        required = {
            d.split(">=")[0].split("==")[0].split(">")[0].strip() for d in project["dependencies"]
        }
        # package name -> import name where they differ
        import_names = {
            "python-dotenv": "dotenv",
            "pytest": "pytest",
        }
        # pytest-asyncio is a pytest plugin: it is loaded by the runner and
        # never imported by our code, so it cannot appear in this scan.
        skip = {"pytest-asyncio"}
        imported = set()
        for path in Path("xli").rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])

        for dep in required:
            if dep in skip:
                continue
            name = import_names.get(dep, dep)
            assert name in imported, f"{dep} is declared required but never imported"

    def test_numpy_is_declared_in_the_embeddings_extra(self, project):
        """vector_store imports it, so the extra must provide it."""
        extras = project["optional-dependencies"]
        assert any(d.startswith("numpy") for d in extras["embeddings"])

    def test_textual_is_not_required(self, project):
        """The shipping TUI is curses; only the legacy UI needs textual."""
        assert not any(d.startswith("textual") for d in project["dependencies"])
        assert any(d.startswith("textual") for d in project["optional-dependencies"]["tui"])

    def test_pyfiglet_is_gone(self, project):
        """Nothing imports it any more."""
        every = list(project["dependencies"])
        for extra in project["optional-dependencies"].values():
            every.extend(extra)
        assert not any(d.startswith("pyfiglet") for d in every)

    def test_all_extra_covers_every_runtime_extra(self, project):
        """`all` is for users who want every feature.

        `dev` is deliberately excluded: pulling pytest and ruff into an end
        user's environment because they asked for "everything" would be wrong.
        """
        extras = project["optional-dependencies"]
        combined = {
            d
            for name, deps in extras.items()
            if name not in ("all", "dev")
            for d in deps
        }
        assert combined <= set(extras["all"])

    def test_dev_extra_is_not_forbidden_from_being_installed(self, project):
        extras = project["optional-dependencies"]
        assert any(d.startswith("pytest") for d in extras["dev"])
        assert "dev" not in extras["all"] and not any(
            d.startswith("ruff") for d in extras["all"]
        )
