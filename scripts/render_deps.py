#!/usr/bin/env python3
"""
Render a dependency graph for XLI modules in the terminal.

Usage:
    python scripts/render_deps.py [module_prefix]

Examples:
    python scripts/render_deps.py              # all xli.* modules
    python scripts/render_deps.py xli.tui      # only TUI-related
    python scripts/render_deps.py xli.cli      # CLI entry point
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 1. Try to get data from MCP server, fallback to local scan
# ---------------------------------------------------------------------------

def get_graph_from_mcp() -> dict[str, list[str]] | None:
    """Try calling the architecture MCP server via internal API."""
    try:
        # If running inside XLI agent context, we could use mcp_call,
        # but as a standalone script we'll just import the server directly.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from xli.mcp.servers.architecture import handle_dependency_graph  # type: ignore
        result = handle_dependency_graph({})
        if isinstance(result, dict):
            return result
    except Exception:
        pass
    return None


def get_graph_local(prefix: str = "xli/") -> dict[str, list[str]]:
    """Fallback: scan Python files locally and extract imports."""
    import ast

    root = Path(__file__).resolve().parent.parent
    graph: dict[str, list[str]] = {}

    for py_file in root.rglob("*.py"):
        rel = py_file.relative_to(root)
        rel_str = str(rel).replace("\\", "/")
        if not rel_str.startswith(prefix) and prefix != "":
            continue
        # Convert path to module name
        module = rel_str.replace("/", ".").removesuffix(".py")
        if module.endswith(".__init__"):
            module = module.removesuffix(".__init__")

        deps: list[str] = []
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        deps.append(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        deps.append(node.module)
        except SyntaxError:
            pass

        graph[module] = sorted(set(deps))

    return graph


# ---------------------------------------------------------------------------
# 2. Build a tree structure from flat graph
# ---------------------------------------------------------------------------

def build_tree(
    graph: dict[str, list[str]],
    root_module: str,
    max_depth: int = 4,
    _visited: set[str] | None = None,
    _depth: int = 0,
) -> dict[str, Any]:
    """Build a nested dict tree from flat dependency graph."""
    if _visited is None:
        _visited = set()

    node: dict[str, Any] = {"name": root_module, "children": []}

    if _depth >= max_depth or root_module in _visited:
        if root_module in _visited:
            node["cycle"] = True
        return node

    _visited.add(root_module)

    # Find dependencies that are also in our graph (internal deps)
    deps = graph.get(root_module, [])
    internal_deps = [d for d in deps if d in graph or any(k.startswith(d) for k in graph)]
    external_deps = [d for d in deps if d not in graph and not any(k.startswith(d) for k in graph)]

    for dep in internal_deps:
        # Find exact match or closest prefix
        matched = dep if dep in graph else None
        if not matched:
            for k in graph:
                if k.startswith(dep):
                    matched = k
                    break
        if matched:
            child = build_tree(graph, matched, max_depth, _visited.copy(), _depth + 1)
            node["children"].append(child)

    for dep in external_deps[:5]:  # Limit external deps shown
        node["children"].append({"name": f"⚙️ {dep}", "children": [], "external": True})

    if len(external_deps) > 5:
        node["children"].append({"name": f"... +{len(external_deps)-5} more", "children": []})

    return node


# ---------------------------------------------------------------------------
# 3. Render with rich (preferred) or pure ASCII
# ---------------------------------------------------------------------------

def render_with_rich(tree: dict[str, Any], title: str = "XLI Dependency Graph") -> None:
    """Render using rich.tree.Tree."""
    from rich.console import Console
    from rich.tree import Tree

    console = Console()

    def add_nodes(rich_tree: Tree, node: dict[str, Any]) -> None:
        name = node["name"]
        if node.get("cycle"):
            label = f"🔄 [dim]{name}[/dim] [yellow](circular)[/]"
        elif node.get("external"):
            label = f"[cyan]{name}[/]"
        else:
            label = f"📦 [bold green]{name}[/]"

        branch = rich_tree.add(label)
        for child in node.get("children", []):
            add_nodes(branch, child)

    root_tree = Tree(f"[bold magenta]🌳 {title}[/]")
    for child in tree.get("children", []):
        add_nodes(root_tree, child)

    # Add root itself
    root_label = f"📦 [bold green]{tree['name']}[/]"
    root_branch = root_tree.add(root_label)
    for child in tree.get("children", []):
        add_nodes(root_branch, child)

    console.print(root_tree)


def render_ascii(tree: dict[str, Any], title: str = "XLI Dependency Graph") -> None:
    """Pure Unicode box-drawing fallback when rich is not installed."""
    print(f"\n{'='*60}")
    print(f"  🌳 {title}")
    print(f"{'='*60}\n")

    def walk(node: dict[str, Any], prefix: str = "", is_last: bool = True) -> None:
        connector = "└── " if is_last else "├── "
        name = node["name"]
        cycle_mark = " 🔄(circular)" if node.get("cycle") else ""

        if prefix == "":
            print(f"  📦 {name}{cycle_mark}")
        else:
            print(f"  {prefix}{connector}{name}{cycle_mark}")

        children = node.get("children", [])
        extension = "    " if is_last else "│   "
        new_prefix = prefix + extension if prefix else ""

        for i, child in enumerate(children):
            walk(child, new_prefix, i == len(children) - 1)

    walk(tree)
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    prefix_filter = sys.argv[1] if len(sys.argv) > 1 else "xli"

    # Get graph data
    graph = get_graph_from_mcp()
    source = "MCP architecture server"

    if not graph:
        graph = get_graph_local(prefix="xli/")
        source = "local AST scan"

    # Filter by prefix
    if prefix_filter:
        filtered = {
            k: v for k, v in graph.items()
            if k.startswith(prefix_filter) or prefix_filter in k
        }
        if filtered:
            graph = filtered

    if not graph:
        print(f"No modules found matching '{prefix_filter}'")
        sys.exit(1)

    # Pick root: either exact match or first module
    root_module = prefix_filter if prefix_filter in graph else next(iter(graph))

    # Build tree
    tree = build_tree(graph, root_module, max_depth=4)

    title = f"Dependencies: {root_module} (source: {source})"

    # Render
    try:
        render_with_rich(tree, title)
    except ImportError:
        render_ascii(tree, title)


if __name__ == "__main__":
    main()
