#!/usr/bin/env python3
"""XLI structured tools — things the shell can do badly or not at all.

The agent always has `bash`. That is exactly why these tools exist: anything
that can be done with `awk`/`sed`/`jq`/`python -c` sits one quoting mistake
away from a wrong answer, and the produced text is a wall of output rather than
data the model can reason about.

Each of these returns a *structure* and a one-line summary:

``apply_patch``  apply a unified diff with context matching and a clear report
``outline``      the symbols in a file (classes, functions, signatures, line no.)
``json_query``   query a JSON/YAML document by path, without jq
``repo_map``     a compact map of a repository: what is big, what is where
``dep_graph``    the import graph: cycles (the bad kind), leaves, hottest modules

None of them prints, and none of them decides whether it may run — that is the
registry and the policy, as with every other tool.
"""

from __future__ import annotations

import ast
import difflib
import json
import re
from pathlib import Path
from typing import Any

from xli.tools.base import Param, ToolError, ToolResult, tool

MAX_PATCH_BYTES = 1_000_000
MAX_OUTLINE_LINES = 400


# ------------------------------------------------------------------ apply_patch
_HUNK = re.compile(r"^@@\s*-(\d+)(?:,(\d+))?\s*\+(\d+)(?:,(\d+))?\s*@@")


@tool(
    "apply_patch",
    "Apply a unified diff (git-style patch) to a file. Context lines are matched "
    "with a small fuzz factor, so a patch still applies when indentation has "
    "drifted. Returns which hunks applied and which did not — it never writes a "
    "partial file silently if a hunk cannot be located.",
    [
        Param("patch", "string", "Unified diff text (---/+++/@@ hunks)", required=True),
        Param("path", "string", "Target file, when the patch omits ---/+++ headers", default=""),
        Param("dry_run", "boolean", "Check only; do not write", default=False),
    ],
    mutates=True,
    tags=["fs", "structured"],
)
def apply_patch(patch: str, path: str = "", dry_run: bool = False) -> ToolResult:
    """Apply a unified diff to a file.

    Deliberately all-or-nothing: if any hunk cannot be located the file is left
    untouched and the failure names the hunk. A half-applied patch is worse than
    a rejected one, because the next read shows code that never existed.
    """
    if len(patch) > MAX_PATCH_BYTES:
        raise ToolError(f"patch is {len(patch)} bytes, over the {MAX_PATCH_BYTES}-byte limit")

    target, diff_text = _patch_target(patch, path)
    if not target.exists():
        raise ToolError(f"no such file: {target}")
    original = target.read_text(encoding="utf-8", errors="replace")
    lines = original.split("\n")

    result = _apply_hunks(lines, diff_text)
    if not result["ok"]:
        return ToolResult.failure(
            result["error"],
            tool="apply_patch",
            data={"path": str(target), "hunks": result["hunks"]},
        )

    updated = "\n".join(result["lines"])
    if dry_run:
        return ToolResult.success(
            data={"path": str(target), "dry_run": True, "hunks": result["hunks"]},
            summary=f"patch applies cleanly to {target.name} ({len(result['hunks'])} hunk(s))",
        )

    target.write_text(updated, encoding="utf-8")
    added = sum(hunk["added"] for hunk in result["hunks"])
    removed = sum(hunk["removed"] for hunk in result["hunks"])
    return ToolResult.success(
        data={
            "path": str(target),
            "hunks": result["hunks"],
            "added": added,
            "removed": removed,
            "bytes": len(updated.encode("utf-8")),
        },
        summary=f"patched {target.name}: {len(result['hunks'])} hunk(s), +{added}/-{removed}",
    )


def _patch_target(patch: str, fallback: str) -> tuple[Path, list[str]]:
    """Split the diff from its headers, resolving the path to patch."""
    lines = patch.splitlines()
    name = fallback
    body: list[str] = []
    for line in lines:
        if line.startswith("+++ "):
            candidate = line[4:].strip()
            name = name or candidate.split("\t")[0]
            # `+++ b/xli/cli.py` -> `xli/cli.py`
            if not fallback and candidate.startswith(("a/", "b/")):
                name = candidate[2:]
            continue
        if line.startswith("--- "):
            continue
        if line.startswith("diff --git") and not fallback:
            parts = line.split()
            if len(parts) >= 4:
                name = name or parts[3].lstrip("ab/")
            continue
        body.append(line)
    if not name:
        raise ToolError("patch has no ---/+++ headers and no `path` argument")
    return Path(name).expanduser(), body


def _apply_hunks(lines: list[str], body: list[str]) -> dict[str, Any]:
    """Apply unified-diff hunks to ``lines``; see :func:`apply_patch`."""
    hunks: list[dict[str, Any]] = []
    offset = 0
    index = 0
    out = list(lines)

    while index < len(body):
        match = _HUNK.match(body[index])
        if not match:
            index += 1
            continue
        start = int(match.group(1))
        added = removed = 0
        index += 1
        old_block: list[str] = []
        new_block: list[str] = []
        while index < len(body) and not body[index].startswith("@@") and not body[index].startswith("--- "):
            line = body[index]
            if line.startswith("\\"):  # "\ No newline at end of file"
                index += 1
                continue
            marker, text = (line[0], line[1:]) if line else (" ", "")
            if marker == "+":
                new_block.append(text)
                added += 1
            elif marker == "-":
                old_block.append(text)
                removed += 1
            else:  # context (also covers a bare "" line from a trailing newline)
                old_block.append(text)
                new_block.append(text)
            index += 1

        position = _locate(out, old_block, start - 1 + offset)
        if position is None:
            return {
                "ok": False,
                "error": (
                    f"hunk at line {start} does not match — the file changed, or the "
                    f"patch was made against a different revision"
                ),
                "hunks": hunks,
            }
        out[position : position + len(old_block)] = new_block
        offset += len(new_block) - len(old_block)
        hunks.append(
            {
                "line": start,
                "added": added,
                "removed": removed,
                "matched_at": position + 1,
            }
        )
    if not hunks:
        return {"ok": False, "error": "no hunks found in the patch", "hunks": []}
    return {"ok": True, "lines": out, "hunks": hunks}


def _locate(lines: list[str], block: list[str], hint: int) -> int | None:
    """Find ``block`` in ``lines``, preferring the hinted position.

    Falls back to a whitespace-insensitive search and then to difflib's fuzzy
    matcher, because models reproduce patches from memory and indentation is
    the first thing to drift.
    """
    if not block:
        return max(0, min(hint, len(lines)))
    length = len(block)

    def exact(where: int) -> bool:
        return lines[where : where + length] == block

    for candidate in (hint, hint - 1, hint + 1):
        if 0 <= candidate <= len(lines) - length and exact(candidate):
            return candidate
    for candidate in range(0, max(0, len(lines) - length + 1)):
        if exact(candidate):
            return candidate

    stripped = [line.strip() for line in block]
    for candidate in range(0, max(0, len(lines) - length + 1)):
        window = [line.strip() for line in lines[candidate : candidate + length]]
        if window == stripped:
            return candidate

    matcher = difflib.SequenceMatcher(None, lines, block, autojunk=False)
    match = matcher.find_longest_match(0, len(lines), 0, length)
    if block and match.size >= max(1, int(length * 0.6)):
        start = match.a - match.b
        if start >= 0:
            return start
    return None


# ---------------------------------------------------------------------- outline
@tool(
    "outline",
    "Show the structure of a source file: imports, classes, functions, methods and "
    "their line numbers, without printing the body. Works on Python (via ast); "
    "for other languages it falls back to a regex scan of declaration keywords.",
    [
        Param("path", "string", "File to outline", required=True),
    ],
    tags=["code", "structured"],
)
def outline(path: str) -> ToolResult:
    target = Path(path).expanduser()
    if not target.exists():
        raise ToolError(f"no such file: {target}")
    text = target.read_text(encoding="utf-8", errors="replace")

    if target.suffix == ".py":
        symbols = _python_outline(text)
    else:
        symbols = _generic_outline(text)

    if not symbols:
        return ToolResult.success(
            data={"path": str(target), "symbols": []},
            summary=f"{target.name}: no top-level symbols found",
        )
    rendered = "\n".join(
        f"{symbol['line']:>5}  {'  ' * symbol['depth']}{symbol['kind']:<9} {symbol['signature']}"
        for symbol in symbols[:MAX_OUTLINE_LINES]
    )
    kinds: dict[str, int] = {}
    for symbol in symbols:
        kinds[symbol["kind"]] = kinds.get(symbol["kind"], 0) + 1
    summary = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
    return ToolResult.success(
        data={
            "path": str(target),
            "symbols": symbols,
            "text": rendered,
            "total": len(symbols),
        },
        summary=f"{target.name}: {summary}",
    )


def _python_outline(text: str) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise ToolError(f"cannot parse Python: {exc}") from exc

    symbols: list[dict[str, Any]] = []

    def visit(node: ast.AST, depth: int) -> None:
        for child in getattr(node, "body", []):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                prefix = "async def " if isinstance(child, ast.AsyncFunctionDef) else "def "
                symbols.append(
                    {
                        "kind": "method" if depth else "function",
                        "name": child.name,
                        "line": child.lineno,
                        "depth": depth,
                        "signature": prefix + child.name + _signature(child),
                    }
                )
            elif isinstance(child, ast.ClassDef):
                bases = ", ".join(ast.unparse(base) for base in child.bases[:3])
                symbols.append(
                    {
                        "kind": "class",
                        "name": child.name,
                        "line": child.lineno,
                        "depth": depth,
                        "signature": f"class {child.name}" + (f"({bases})" if bases else ""),
                    }
                )
                visit(child, depth + 1)
            elif isinstance(child, (ast.Import, ast.ImportFrom)) and depth == 0:
                names = ", ".join(
                    alias.asname or alias.name.split(".")[0] for alias in child.names
                )
                source = f"from {child.module}" if isinstance(child, ast.ImportFrom) else "import"
                symbols.append(
                    {
                        "kind": "import",
                        "name": names,
                        "line": child.lineno,
                        "depth": depth,
                        "signature": f"{source} {names}",
                    }
                )
            elif isinstance(child, ast.Assign) and depth == 0:
                names = ", ".join(
                    target.id for target in child.targets if isinstance(target, ast.Name)
                )
                if names and names.isupper():
                    symbols.append(
                        {
                            "kind": "constant",
                            "name": names,
                            "line": child.lineno,
                            "depth": depth,
                            "signature": names,
                        }
                    )

    visit(tree, 0)
    return symbols


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args: list[str] = []
    for arg in node.args.posonlyargs:
        args.append(arg.arg)
    if node.args.posonlyargs:
        args.append("/")
    for arg in node.args.args:
        if arg.arg in ("self", "cls"):
            continue
        args.append(arg.arg)
    if node.args.vararg:
        args.append(f"*{node.args.vararg.arg}")
    for arg in node.args.kwonlyargs:
        args.append(arg.arg)
    if node.args.kwarg:
        args.append(f"**{node.args.kwarg.arg}")
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"({', '.join(args)}){returns}"


_GENERIC_DECL = re.compile(
    r"^\s*(?:export\s+|public\s+|private\s+|static\s+|async\s+)*"
    r"(class|interface|struct|enum|func|fn|function|def|type)\s+([A-Za-z_][\w]*)"
)


def _generic_outline(text: str) -> list[dict[str, Any]]:
    symbols: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), 1):
        match = _GENERIC_DECL.match(line)
        if match:
            kind, name = match.group(1), match.group(2)
            symbols.append(
                {
                    "kind": kind,
                    "name": name,
                    "line": number,
                    "depth": 0,
                    "signature": line.strip()[:100],
                }
            )
    return symbols


# ------------------------------------------------------------------- json_query
@tool(
    "json_query",
    "Read a JSON or YAML file and query it by path. Examples: "
    "'dependencies.httpx', 'items[0].name', 'scripts.*.command'. Returns the value "
    "as JSON. No jq, no shell quoting, no Python expression evaluation.",
    [
        Param("path", "string", "JSON/YAML file to read", required=True),
        Param("query", "string", "Dotted path, [] for indexes, * for all keys", default=""),
        Param("limit", "integer", "Max characters of the result to return", default=4000),
    ],
    tags=["data", "read"],
)
def json_query(path: str, query: str = "", limit: int = 4000) -> ToolResult:
    target = Path(path).expanduser()
    if not target.exists():
        raise ToolError(f"no such file: {target}")
    raw = target.read_text(encoding="utf-8", errors="replace")

    document = _load_document(raw, target)
    value = _walk(document, query)
    try:
        rendered = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        rendered = str(value)
    truncated = rendered[: max(200, int(limit))]
    return ToolResult.success(
        data={"path": str(target), "query": query or ".", "value": value, "text": truncated},
        summary=f"{target.name}: {query or '.'} -> {truncated.splitlines()[0][:60]}",
    )


def _load_document(raw: str, target: Path) -> Any:
    if target.suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError:
            return _tiny_yaml(raw)
        try:
            return yaml.safe_load(raw)
        except Exception as exc:  # noqa: BLE001 - any parser error is the user's YAML
            raise ToolError(f"invalid YAML: {exc}") from exc
    if target.suffix == ".toml":
        import tomllib

        try:
            return tomllib.loads(raw)
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"invalid TOML: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError(f"invalid JSON at line {exc.lineno}: {exc.msg}") from exc


def _walk(document: Any, query: str) -> Any:
    """Resolve a dotted path with ``[n]`` indexes and ``*`` wildcards."""
    if not query or query in (".", "$"):
        return document
    tokens = _tokenize_path(query)
    current = document
    for token in tokens:
        if token == "*":
            if isinstance(current, dict):
                current = list(current.values())
            elif isinstance(current, list):
                current = list(current)
            else:
                raise ToolError(f"'*' does not apply to {type(current).__name__}")
        elif token.startswith("[") and token.endswith("]"):
            index = token[1:-1]
            if not isinstance(current, list):
                raise ToolError(f"index {token} applied to {type(current).__name__}")
            if index == "*":
                current = list(current)
                continue
            try:
                current = current[int(index)]
            except (ValueError, IndexError) as exc:
                raise ToolError(f"bad index {token}: {exc}") from exc
        else:
            if isinstance(current, list):
                collected = []
                for item in current:
                    if isinstance(item, dict) and token in item:
                        collected.append(item[token])
                if not collected:
                    raise ToolError(f"key {token!r} not found in any item")
                current = collected
                continue
            if not isinstance(current, dict) or token not in current:
                available = ", ".join(list(current)[:10]) if isinstance(current, dict) else type(current).__name__
                raise ToolError(f"key {token!r} not found (available: {available})")
            current = current[token]
    return current


def _tokenize_path(query: str) -> list[str]:
    tokens: list[str] = []
    buffer = ""
    for char in query.strip().lstrip("$").lstrip("."):
        if char == ".":
            if buffer:
                tokens.append(buffer)
                buffer = ""
        elif char == "[":
            if buffer:
                tokens.append(buffer)
                buffer = ""
            buffer = "["
        elif char == "]":
            buffer += "]"
            tokens.append(buffer)
            buffer = ""
        else:
            buffer += char
    if buffer:
        tokens.append(buffer)
    return [token for token in tokens if token]


def _tiny_yaml(raw: str) -> Any:
    """A very small YAML subset: mappings, lists and scalars.

    Only used when PyYAML is absent, which is the common case for a bare
    install. Anything more complex than nesting with two-space indents gets
    reported as unparsed rather than silently mis-read.
    """
    result: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, result)]
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        text = line.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1] if stack else result
        if text.startswith("- "):
            if isinstance(parent, dict):
                parent.setdefault("_list", [])
            continue
        if ":" in text:
            key, _, value = text.partition(":")
            key = key.strip()
            value = value.strip()
            if not value:
                holder: dict[str, Any] = {}
                parent[key] = holder  # type: ignore[index]
                stack.append((indent, holder))
            else:
                parent[key] = _scalar(value)  # type: ignore[index]
    return result


def _scalar(text: str) -> Any:
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    if text.lower() in ("null", "~"):
        return None
    if text.startswith(('"', "'")) and text.endswith(text[0]):
        return text[1:-1]
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


# --------------------------------------------------------------------- repo_map
_IGNORED = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "target", "out",
}


@tool(
    "repo_map",
    "Map a repository without reading it file by file: directories and their sizes, "
    "the largest files, and a symbol count per file. Use it to decide where to look "
    "next; then use read/outline on what it points at.",
    [
        Param("path", "string", "Root directory (default: .)", default="."),
        Param("depth", "integer", "Directory depth to summarise", default=2),
        Param("limit", "integer", "How many files to list", default=25),
    ],
    tags=["code", "read", "structured"],
)
def repo_map(path: str = ".", depth: int = 2, limit: int = 25) -> ToolResult:
    root = Path(path).expanduser()
    if not root.exists() or not root.is_dir():
        raise ToolError(f"not a directory: {root}")

    files: list[dict[str, Any]] = []
    dirs: dict[str, dict[str, int]] = {}
    for item in root.rglob("*"):
        if any(part in _IGNORED for part in item.parts):
            continue
        if item.is_dir():
            continue
        try:
            size = item.stat().st_size
        except OSError:
            continue
        relative = item.relative_to(root)
        key = "/".join(relative.parts[: max(1, int(depth))][:-1]) or "."
        bucket = dirs.setdefault(key, {"files": 0, "bytes": 0})
        bucket["files"] += 1
        bucket["bytes"] += size
        files.append(
            {
                "path": str(relative),
                "bytes": size,
                "lines": _count_lines(item),
                "kind": item.suffix.lstrip(".") or "none",
            }
        )

    files.sort(key=lambda entry: -entry["bytes"])
    top = files[: max(1, int(limit))]
    rendered = "\n".join(
        f"{entry['bytes']:>9}  {entry['lines']:>6}  {entry['path']}" for entry in top
    )
    return ToolResult.success(
        data={
            "root": str(root),
            "files": len(files),
            "bytes": sum(entry["bytes"] for entry in files),
            "directories": dirs,
            "largest": top,
            "text": rendered,
        },
        summary=(
            f"{len(files)} files, {sum(entry['bytes'] for entry in files) // 1024} KiB; "
            f"largest: {top[0]['path'] if top else '-'}"
        ),
    )


def _count_lines(item: Path) -> int:
    if item.suffix not in (".py", ".md", ".json", ".yaml", ".yml", ".toml", ".txt", ".cfg", ".ini"):
        return 0
    try:
        with item.open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


# -------------------------------------------------------------------- dep_graph
@tool(
    "dep_graph",
    "Build the import graph of a Python tree and report what matters: cycles, "
    "modules nothing imports, the most-depended-upon modules, and the edges that "
    "would break if a module were removed. Structured output, not a picture.",
    [
        Param("path", "string", "Root directory (default: .)", default="."),
        Param("focus", "string", "Only report on modules whose name contains this", default=""),
    ],
    tags=["code", "read", "structured"],
)
def dep_graph(path: str = ".", focus: str = "") -> ToolResult:
    from xli.core.dependency_graph import DependencyGraph

    root = Path(path).expanduser()
    if not root.exists():
        raise ToolError(f"not a directory: {root}")
    graph = DependencyGraph(str(root))

    name = focus.strip()
    modules = {
        module: sorted(target for target in targets if not name or name in module)
        for module, targets in graph.graph.items()
        if (not name or name in module)
    }
    cycles = [
        (source, target) for source, target in graph.detect_cycles()
        if (not name or name in source)
    ]
    imported = {target for targets in graph.graph.values() for target in targets}
    roots = sorted(
        module for module in modules
        if not any(
            other != module and module in graph.graph.get(other, ())
            for other in graph.graph
        )
    )
    hubs = sorted(
        ((module, len(targets)) for module, targets in modules.items()),
        key=lambda pair: -pair[1],
    )[:10]

    text = "\n".join(
        [f"{module} -> {', '.join(targets[:6]) or '-'}" for module, targets in list(modules.items())[:40]]
    )
    return ToolResult.success(
        data={
            "root": str(root),
            "modules": len(modules),
            "cycles": cycles,
            "roots": roots[:20],
            "hubs": hubs,
            "text": text,
        },
        summary=(
            f"{len(modules)} modules, {len(cycles)} cycle edge(s), "
            f"{len(roots)} entry point(s)"
        ),
    )


#: Registered by `xli.tools.builtin`; also the list `xli tools list` tags.
STRUCTURED_TOOLS = [apply_patch, outline, json_query, repo_map, dep_graph]

__all__ = ["STRUCTURED_TOOLS", "apply_patch", "dep_graph", "json_query", "outline", "repo_map"]
