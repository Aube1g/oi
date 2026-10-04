#!/usr/bin/env python3
"""MCP Codebase server — symbols, references, imports and call sites.

`knowledge` searches file text, which answers "where does this word appear".
The questions an agent actually asks are narrower and need a parser:

    * where is `Foo.bar` *defined*, and at which line
    * who calls it — the call sites, not the docstring
    * what does this module import, and who imports this module
    * what is in this file, in order, with line numbers

Answers are computed with `ast` and cached by file mtime, so a repeated
question about a big tree does not re-parse it. Python is parsed properly;
other languages fall back to a regex scan, which is honest about being one.
"""

import ast
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

from xli.mcp.serverkit import filter_arguments, shake_hands, tool_descriptors

SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", "env",
    "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "target",
    ".next", ".idea", ".vscode",
}
SOURCE_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".php",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".kt", ".swift", ".lua", ".sh",
    ".sql", ".yaml", ".yml", ".toml", ".json", ".md",
}
CODE_SUFFIXES = SOURCE_SUFFIXES - {".json", ".md", ".yaml", ".yml", ".toml"}

_DEFINITION_PATTERNS = (
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?def\s+(?P<name>\w+)"),
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(?P<name>\w+)"),
    re.compile(r"^\s*(?:export\s+)?class\s+(?P<name>\w+)"),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>\w+)\s*="),
    re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(?P<name>\w+)"),
    re.compile(r"^\s*(?:func)\s+(?P<name>\w+)"),
    re.compile(r"^\s*struct\s+(?P<name>\w+)"),
)

_ast_cache: dict[str, tuple[float, ast.Module | None, str]] = {}


# ------------------------------------------------------------------ helpers
def _root(path: str) -> Path:
    root = Path(path or ".").expanduser().resolve()
    if not root.exists():
        raise RuntimeError(f"Путь не существует: {root}")
    return root


def _iter_files(root: Path, suffixes: set[str] | None = None):
    suffixes = suffixes or SOURCE_SUFFIXES
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS and not name.startswith(".")]
        for filename in filenames:
            candidate = Path(directory) / filename
            if candidate.suffix in suffixes or filename in ("Makefile", "Dockerfile"):
                yield candidate


def _parsed(path: Path) -> ast.Module | None:
    """Cache the AST by mtime — an agent asks about the same files repeatedly."""
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    key = str(path)
    cached = _ast_cache.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError, OSError):
        tree = None
    if len(_ast_cache) > 2000:
        _ast_cache.clear()
    _ast_cache[key] = (stamp, tree, "")
    return tree


def _relative(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _node_source(path: Path, node: ast.AST) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    start = max((getattr(node, "lineno", 1) or 1) - 1, 0)
    end = getattr(node, "end_lineno", start + 1) or start + 1
    return "\n".join(lines[start:end])


# -------------------------------------------------------------------- tools
def find_symbol(symbol: str, path: str = ".", limit: int = 40) -> str:
    """Where a symbol is defined: file, line, kind and its signature."""
    root = _root(path)
    wanted = (symbol or "").strip()
    if not wanted:
        raise RuntimeError("Нужно имя символа (symbol).")
    leaf = wanted.rsplit(".", 1)[-1]

    hits: list[str] = []
    for file in _iter_files(root, CODE_SUFFIXES):
        tree = _parsed(file) if file.suffix == ".py" else None
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name == leaf:
                        kind = "class" if isinstance(node, ast.ClassDef) else "def"
                        hits.append(
                            f"{_relative(root, file)}:{node.lineno}  {kind} {node.name}"
                            f"  ({_node_source(file, node).splitlines()[0].strip()[:120]})"
                        )
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == leaf:
                            hits.append(f"{_relative(root, file)}:{node.lineno}  var {target.id}")
        else:
            for number, line in enumerate(
                file.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
            ):
                for pattern in _DEFINITION_PATTERNS:
                    match = pattern.match(line)
                    if match and match.group("name") == leaf:
                        hits.append(f"{_relative(root, file)}:{number}  {line.strip()[:120]}")
                        break
        if len(hits) >= limit:
            break

    if not hits:
        return f"Символ {wanted!r} не найден под {root}."
    return "\n".join([f"Определения {wanted}: {len(hits)}", *hits[:limit]])


def find_usages(symbol: str, path: str = ".", limit: int = 60) -> str:
    """Call sites and references to a symbol, with the surrounding line."""
    root = _root(path)
    leaf = (symbol or "").strip().rsplit(".", 1)[-1]
    if not leaf:
        raise RuntimeError("Нужно имя символа (symbol).")

    # Word-boundary match that does not count the definition itself.
    pattern = re.compile(rf"(?<![\w.]){re.escape(leaf)}\s*\(")
    bare = re.compile(rf"(?<![\w.]){re.escape(leaf)}(?![\w])")
    definition = re.compile(rf"^\s*(?:async\s+)?(?:def|class)\s+{re.escape(leaf)}\b")

    calls: list[str] = []
    mentions: list[str] = []
    for file in _iter_files(root, CODE_SUFFIXES):
        try:
            lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, start=1):
            if definition.match(line):
                continue
            entry = f"{_relative(root, file)}:{number}  {line.strip()[:140]}"
            if pattern.search(line):
                calls.append(entry)
            elif bare.search(line):
                mentions.append(entry)
        if len(calls) >= limit:
            break

    if not calls and not mentions:
        return f"Упоминаний {leaf!r} не найдено под {root}."
    out = [f"Вызовы {leaf}: {len(calls)}"]
    out.extend(calls[:limit])
    if mentions:
        out.append(f"Прочие упоминания: {len(mentions)}")
        out.extend(mentions[: max(0, limit - len(calls))])
    return "\n".join(out)


def module_imports(path: str) -> str:
    """What one file imports, with the names it pulls in."""
    target = _root(path) if Path(path).is_dir() else Path(path).expanduser().resolve()
    if not target.exists():
        raise RuntimeError(f"Файл не найден: {target}")
    tree = _parsed(target)
    if tree is None:
        return f"{target.name}: не Python-файл или не разбирается как Python."

    out: list[str] = [f"{target.name} imports:"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append(f"  import {alias.name}" + (f" as {alias.asname}" if alias.asname else ""))
        elif isinstance(node, ast.ImportFrom):
            names = ", ".join(alias.name + (f" as {alias.asname}" if alias.asname else "") for alias in node.names)
            out.append(f"  from {'.' * node.level}{node.module or ''} import {names}")
    return "\n".join(out)


def importers_of(module: str, path: str = ".", limit: int = 60) -> str:
    """Who imports a module — the first question of every refactor."""
    root = _root(path)
    needle = (module or "").strip()
    if not needle:
        raise RuntimeError("Нужно имя модуля (module), например xli.parse.")
    leaf = needle.rsplit(".", 1)[-1]

    hits: list[str] = []
    for file in _iter_files(root, {".py"}):
        tree = _parsed(file)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == needle or alias.name.endswith("." + leaf):
                        hits.append(f"{_relative(root, file)}:{node.lineno}  import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                imported = node.module or ""
                if imported == needle or imported.endswith("." + leaf) or any(
                    alias.name == leaf for alias in node.names
                ):
                    hits.append(
                        f"{_relative(root, file)}:{node.lineno}  from {imported} import "
                        + ", ".join(alias.name for alias in node.names[:6])
                    )
        if len(hits) >= limit:
            break
    if not hits:
        return f"Модуль {needle!r} никто не импортирует под {root}."
    return "\n".join([f"Импортируют {needle}: {len(hits)} файлов/строк", *hits[:limit]])


def outline(path: str, limit: int = 200) -> str:
    """The structure of one file: imports, classes, functions, with line numbers."""
    target = Path(path).expanduser().resolve()
    if not target.exists():
        raise RuntimeError(f"Файл не найден: {target}")
    tree = _parsed(target)
    if tree is None:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(
            f"{number}: {line.strip()[:120]}"
            for number, line in enumerate(lines[:limit], start=1)
            if any(pattern.match(line) for pattern in _DEFINITION_PATTERNS)
        ) or "(объявление не найдено)"

    out: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = ", ".join(arg.arg for arg in node.args.args)
            out.append(f"{node.lineno}: def {node.name}({args})")
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(base) for base in node.bases)
            out.append(f"{node.lineno}: class {node.name}({bases})")
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = ", ".join(arg.arg for arg in child.args.args)
                    out.append(f"{child.lineno}:     def {child.name}({args})")
        elif isinstance(node, ast.Assign) and len(out) < limit:
            for target_node in node.targets:
                if isinstance(target_node, ast.Name) and target_node.id.isupper():
                    out.append(f"{node.lineno}: {target_node.id} = ...")
    return "\n".join(out[:limit]) if out else "(файл без объявлений)"


def overview(path: str = ".", limit: int = 40) -> str:
    """A map of the tree: languages, sizes, the biggest files, entry points."""
    root = _root(path)
    by_suffix: dict[str, list[int]] = defaultdict(list)
    total_bytes = 0
    file_count = 0
    for file in _iter_files(root, SOURCE_SUFFIXES):
        try:
            size = file.stat().st_size
        except OSError:
            continue
        by_suffix[file.suffix or file.name].append(size)
        total_bytes += size
        file_count += 1

    lines = [f"{root}", f"Файлов: {file_count}, объём: {total_bytes / 1024:.0f} КиБ"]
    for suffix, sizes in sorted(by_suffix.items(), key=lambda item: -sum(item[1]))[:12]:
        lines.append(f"  {suffix or '?':>8}: {len(sizes):5} файлов, {sum(sizes) / 1024:8.0f} КиБ")

    biggest = sorted(
        ((file.stat().st_size, _relative(root, file)) for file in _iter_files(root, SOURCE_SUFFIXES)),
        reverse=True,
    )[:limit]
    lines.append("Крупнейшие файлы:")
    lines.extend(f"  {size / 1024:8.0f} КиБ  {name}" for size, name in biggest)

    for candidate in ("__main__.py", "main.py", "cli.py", "app.py", "manage.py", "index.js", "main.go"):
        for match in root.rglob(candidate):
            if not any(part in SKIP_DIRS for part in match.parts):
                lines.append(f"Точка входа: {_relative(root, match)}")
                break
    return "\n".join(lines)


def search_code(query: str, path: str = ".", limit: int = 30) -> str:
    """Search file contents with a regular expression, newest files first."""
    root = _root(path)
    if not query:
        raise RuntimeError("Пустой поисковый запрос (query).")
    try:
        pattern = re.compile(query)
    except re.error:
        pattern = re.compile(re.escape(query))

    hits: list[str] = []
    for file in _iter_files(root):
        try:
            lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, start=1):
            if pattern.search(line):
                hits.append(f"{_relative(root, file)}:{number}  {line.strip()[:150]}")
                if len(hits) >= limit:
                    return "\n".join(hits)
    return "\n".join(hits) if hits else f"Совпадений нет: {query!r}"


def read_slice(path: str, start: int = 1, end: int = 80) -> str:
    """Read a numbered line range — cheaper than a whole file for a big one."""
    target = Path(path).expanduser().resolve()
    if not target.exists():
        raise RuntimeError(f"Файл не найден: {target}")
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    first = max(1, int(start))
    last = min(len(lines), max(first, int(end)))
    width = len(str(last))
    return "\n".join(f"{number:>{width}}: {lines[number - 1]}" for number in range(first, last + 1))


def stats(path: str = ".") -> str:
    """Counts per language and the test/source ratio."""
    root = _root(path)
    code = tests = 0
    code_lines = test_lines = 0
    for file in _iter_files(root, CODE_SUFFIXES):
        try:
            line_count = len(file.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
        name = str(file).lower()
        if "test" in name or "spec" in name:
            tests += 1
            test_lines += line_count
        else:
            code += 1
            code_lines += line_count
    started = time.time()
    ratio = f"{code_lines / max(test_lines, 1):.1f}:1" if test_lines else "нет тестов"
    return (
        f"Файлов кода: {code} ({code_lines} строк)\n"
        f"Файлов тестов: {tests} ({test_lines} строк)\n"
        f"Соотношение код:тесты = {ratio}\n"
        f"Посчитано за {time.time() - started:.2f} с"
    )


TOOLS = {
    "overview": overview,
    "find_symbol": find_symbol,
    "find_usages": find_usages,
    "module_imports": module_imports,
    "importers_of": importers_of,
    "outline": outline,
    "search_code": search_code,
    "read_slice": read_slice,
    "stats": stats,
}


def handle_request(request):
    # initialize / ping / notifications, per the protocol
    early = shake_hands(request)
    if early is not None:
        return early
    method = request.get("method")
    request_id = request.get("id")

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tool_descriptors(TOOLS)}}
    if method == "tools/call":
        params = request.get("params") or {}
        tool = params.get("name")
        if tool not in TOOLS:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Нет такого инструмента: {tool}"},
            }
        try:
            result = TOOLS[tool](**filter_arguments(TOOLS[tool], params.get("arguments") or {}))
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": str(result)}], "isError": False},
            }
        except Exception as exc:  # noqa: BLE001 - the error text is the result
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    "isError": True,
                },
            }
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Нет такого метода: {method}"},
    }


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            reply = handle_request(request)
            if reply is not None:
                print(json.dumps(reply, ensure_ascii=False), flush=True)
        except Exception as exc:  # noqa: BLE001 - one bad line must not stop the server
            print(
                json.dumps(
                    {"jsonrpc": "2.0", "error": {"code": -32700, "message": str(exc)}},
                    ensure_ascii=False,
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
