#!/usr/bin/env python3
"""
XLI Built-in tools — read, write, edit, search, shell, git, todo.

Every tool here is a pure function of its arguments plus the filesystem. None of
them print, and none of them decide whether they are permitted to run.

The `edit` tool deserves a note: it applies a fuzzy match, so a model that
reproduces the target text with slightly different indentation still succeeds —
but only when the match is unique. An ambiguous or absent match is an error,
never a silent wrong write.
"""

from __future__ import annotations

import difflib
import fnmatch
import re
import subprocess

from xli.core.exec_guard import run_guarded_shell
from xli.core.shell_safety import is_shell_command_safe
from xli.tools.structured import STRUCTURED_TOOLS
from xli.tools.web_search import WEB_SEARCH_TOOLS
from pathlib import Path
from typing import Any

from xli.tools.charts_tool import CHART_TOOL
from xli.tools.base import Param, Tool, ToolError, ToolResult, tool

MAX_READ_BYTES = 2_000_000
MAX_OUTPUT_CHARS = 20_000


# --------------------------------------------------------------------- helpers
def _resolve(path: str) -> Path:
    return Path(path).expanduser()


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n... [{len(text) - limit} chars truncated] ...\n{tail}"


def _count_lines(text: str) -> int:
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


# ------------------------------------------------------------------------ read
@tool(
    "read",
    "Read a text file. Returns numbered lines so later edits can cite them.",
    [
        Param("path", "string", "File path (absolute or relative)", required=True),
        Param("offset", "integer", "1-based first line to return", default=1),
        Param("limit", "integer", "Maximum lines to return", default=2000),
    ],
    tags=["fs"],
)
def read_file(path: str, offset: int = 1, limit: int = 2000) -> ToolResult:
    target = _resolve(path)
    if not target.exists():
        raise ToolError(f"no such file: {target}")
    if target.is_dir():
        raise ToolError(f"{target} is a directory — use ls")

    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        raise ToolError(
            f"{target.name} is {size} bytes, over the {MAX_READ_BYTES}-byte read limit"
        )

    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ToolError(f"cannot read {target}: {exc}") from exc

    lines = raw.splitlines()
    total = len(lines)
    start = max(1, int(offset))
    window = lines[start - 1 : start - 1 + max(1, int(limit))]

    width = len(str(start + len(window) - 1))
    numbered = "\n".join(
        f"{start + i:>{width}} | {line}" for i, line in enumerate(window)
    )

    return ToolResult.success(
        data={"path": str(target), "total_lines": total, "content": numbered},
        summary=f"read {target} ({total} lines, showing {len(window)})",
    )


# ----------------------------------------------------------------------- write
@tool(
    "write",
    "Write a file, creating parent directories. Overwrites existing content.",
    [
        Param("path", "string", "File path", required=True),
        Param("content", "string", "Full file content", required=True),
    ],
    mutates=True,
    tags=["fs"],
)
def write_file(path: str, content: str) -> ToolResult:
    target = _resolve(path)
    if target.is_dir():
        raise ToolError(f"{target} is a directory")

    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    previous = target.stat().st_size if existed else 0

    try:
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"cannot write {target}: {exc}") from exc

    verb = "overwrote" if existed else "created"
    return ToolResult.success(
        data={
            "path": str(target),
            "bytes": len(content.encode("utf-8")),
            "lines": _count_lines(content),
            "previous_bytes": previous,
        },
        summary=f"{verb} {target} ({_count_lines(content)} lines)",
    )


# ------------------------------------------------------------------------ edit
FUZZY_THRESHOLD = 0.85
#: Two candidates closer than this are treated as a tie, and refused.
FUZZY_TIE_EPSILON = 1e-6


def _normalize(line: str) -> str:
    """Collapse runs of whitespace and drop leading indentation."""
    return re.sub(r"\s+", " ", line).strip()


def _fuzzy_find(lines: list[str], needle: list[str]) -> tuple[int, int, float]:
    """Locate `needle` in `lines`, tolerating whitespace differences.

    Returns (start, end, ratio) where ratio is measured against the *original*
    file text, so 1.0 genuinely means a byte-identical match.

    Raises ToolError when the target is absent, or when more than one place
    matches equally well — ambiguity is reported, never guessed at.
    """
    if len(needle) > len(lines):
        raise ToolError("target text not found in file (longer than the file)")

    # 1. Exact matches — but all of them, so duplicates are caught.
    exact = [i for i in range(len(lines) - len(needle) + 1) if lines[i : i + len(needle)] == needle]
    if len(exact) == 1:
        return exact[0], exact[0] + len(needle), 1.0
    if len(exact) > 1:
        raise ToolError(
            f"target text is ambiguous — it occurs {len(exact)} times; "
            "include more surrounding context"
        )

    # 2. Whitespace-insensitive matches, ranked by similarity to the real text.
    norm_needle = [_normalize(line) for line in needle]
    candidates: list[tuple[int, int, float]] = []
    for i in range(len(lines) - len(needle) + 1):
        window = lines[i : i + len(needle)]
        if [_normalize(line) for line in window] != norm_needle:
            continue
        # The normalised forms agree; score how far the raw text actually is.
        raw_ratio = difflib.SequenceMatcher(None, needle, window).ratio()
        candidates.append((i, i + len(needle), raw_ratio))

    if not candidates:
        # 3. Last resort: tolerate small textual drift, not just whitespace.
        for i in range(len(lines) - len(needle) + 1):
            window = lines[i : i + len(needle)]
            ratio = difflib.SequenceMatcher(None, norm_needle, [_normalize(x) for x in window]).ratio()
            if ratio >= FUZZY_THRESHOLD:
                candidates.append((i, i + len(needle), ratio))

    if not candidates:
        raise ToolError("target text not found in file (checked exact and whitespace-fuzzy)")

    candidates.sort(key=lambda c: -c[2])
    if len(candidates) > 1 and candidates[1][2] >= candidates[0][2] - FUZZY_TIE_EPSILON:
        raise ToolError(
            f"target text is ambiguous — {len(candidates)} near-identical matches; "
            "include more surrounding context"
        )
    return candidates[0]


@tool(
    "edit",
    "Replace an exact block of text in a file. Whitespace differences are "
    "tolerated, but the match must be unique.",
    [
        Param("path", "string", "File path", required=True),
        Param("old_text", "string", "Text to find", required=True),
        Param("new_text", "string", "Replacement text", required=True),
    ],
    mutates=True,
    tags=["fs"],
)
def edit_file(path: str, old_text: str, new_text: str) -> ToolResult:
    target = _resolve(path)
    if not target.exists():
        raise ToolError(f"no such file: {target}")
    if old_text == new_text:
        raise ToolError("old_text and new_text are identical — nothing to do")

    original = target.read_text(encoding="utf-8", errors="replace")
    lines = original.splitlines()
    needle = old_text.splitlines()

    if not needle:
        raise ToolError("old_text is empty")

    start, end, ratio = _fuzzy_find(lines, needle)
    replacement = new_text.splitlines()
    updated = lines[:start] + replacement + lines[end:]

    target.write_text("\n".join(updated) + ("\n" if original.endswith("\n") else ""), encoding="utf-8")

    return ToolResult.success(
        data={
            "path": str(target),
            "lines_removed": end - start,
            "lines_added": len(replacement),
            "match_ratio": round(ratio, 4),
        },
        summary=f"edited {target} (-{end - start}/+{len(replacement)} lines"
                + ("" if ratio == 1.0 else f", fuzzy {ratio:.2f})"),
    )


# -------------------------------------------------------------------------- ls
@tool(
    "ls",
    "List a directory, optionally recursive, with sizes. Skips ignored noise.",
    [
        Param("path", "string", "Directory path", default="."),
        Param("recursive", "boolean", "Walk subdirectories", default=False),
        Param("pattern", "string", "Glob filter, e.g. **/*.py", default=""),
    ],
    tags=["fs"],
)
def list_dir(path: str = ".", recursive: bool = False, pattern: str = "") -> ToolResult:
    root = _resolve(path)
    if not root.exists():
        raise ToolError(f"no such directory: {root}")
    if root.is_file():
        raise ToolError(f"{root} is a file — use read")

    skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".tox"}
    entries: list[dict[str, Any]] = []

    def walk(directory: Path) -> None:
        try:
            children = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except PermissionError:
            return
        for child in children:
            if child.is_dir():
                if child.name in skip_dirs:
                    continue
                entries.append({"path": str(child.relative_to(root)), "dir": True, "size": 0})
                if recursive and len(entries) < 5000:
                    walk(child)
            else:
                entries.append(
                    {
                        "path": str(child.relative_to(root)),
                        "dir": False,
                        "size": child.stat().st_size,
                    }
                )

    walk(root)

    if pattern:
        entries = [e for e in entries if fnmatch.fnmatch(e["path"], pattern)]

    return ToolResult.success(
        data={"path": str(root), "entries": entries[:5000]},
        summary=f"{len(entries)} entries under {root}",
    )


# ------------------------------------------------------------------------ glob
@tool(
    "glob",
    "Find files matching a glob pattern, newest first.",
    [
        Param("pattern", "string", "Glob, e.g. **/*.py", required=True),
        Param("path", "string", "Directory to search in", default="."),
    ],
    tags=["fs"],
)
def glob_files(pattern: str, path: str = ".") -> ToolResult:
    root = _resolve(path)
    if not root.is_dir():
        raise ToolError(f"not a directory: {root}")

    matches = [p for p in root.glob(pattern) if p.is_file()]
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    return ToolResult.success(
        data={"pattern": pattern, "root": str(root), "matches": [str(m) for m in matches[:2000]]},
        summary=f"{len(matches)} file(s) matched {pattern}",
    )


# ------------------------------------------------------------------------ grep
@tool(
    "grep",
    "Search file contents with a regex. Returns matching lines with context.",
    [
        Param("pattern", "string", "Regular expression", required=True),
        Param("path", "string", "File or directory", default="."),
        Param("glob", "string", "Limit to matching filenames, e.g. *.py", default=""),
        Param("context", "integer", "Lines of context each side", default=0),
    ],
    tags=["search"],
)
def grep(pattern: str, path: str = ".", glob: str = "", context: int = 0) -> ToolResult:
    root = _resolve(path)
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"invalid regex: {exc}") from exc

    files: list[Path] = []
    if root.is_file():
        files = [root]
    else:
        skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"}
        for candidate in root.rglob("*"):
            if not candidate.is_file():
                continue
            if any(part in skip_dirs for part in candidate.parts):
                continue
            if glob and not fnmatch.fnmatch(candidate.name, glob):
                continue
            files.append(candidate)

    hits: list[dict[str, Any]] = []
    for file in sorted(files):
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if regex.search(line):
                low = max(0, index - context)
                high = min(len(lines), index + context + 1)
                hits.append(
                    {
                        "path": str(file),
                        "line": index + 1,
                        "text": line.strip(),
                        "context": lines[low:high] if context else [],
                    }
                )
                if len(hits) >= 1000:
                    break
        if len(hits) >= 1000:
            break

    return ToolResult.success(
        data={"pattern": pattern, "matches": hits},
        summary=f"{len(hits)} match(es) for /{pattern}/ in {len(files)} file(s)",
    )


# ------------------------------------------------------------------------ bash
@tool(
    "bash",
    "Run a shell command and return stdout, stderr and the exit code.",
    [
        Param("command", "string", "Shell command to run", required=True),
        Param("cwd", "string", "Working directory", default=""),
        Param("timeout", "integer", "Seconds before the command is killed", default=60),
    ],
    mutates=True,
    tags=["shell"],
)
def bash(command: str, cwd: str = "", timeout: int = 60) -> ToolResult:
    if not command.strip():
        raise ToolError("empty command")

    workdir = str(_resolve(cwd)) if cwd else None
    if workdir and not Path(workdir).is_dir():
        raise ToolError(f"no such working directory: {workdir}")

    # Defense in depth behind the permission policy: one blocklist and one
    # guarded runner for the whole tree, so secrets are scrubbed from the
    # child environment and memory/CPU limits actually apply.
    safe, reason = is_shell_command_safe(command)
    if not safe:
        raise ToolError(f"blocked by shell safety: {reason}")

    try:
        proc = run_guarded_shell(command, timeout=max(1, int(timeout)), cwd=workdir)
    except subprocess.TimeoutExpired:
        raise ToolError(f"command timed out after {timeout}s: {command}") from None
    except ValueError as exc:
        raise ToolError(str(exc)) from None

    stdout = _truncate(proc.stdout or "")
    stderr = _truncate(proc.stderr or "")

    return ToolResult(
        ok=proc.returncode == 0,
        data={"exit_code": proc.returncode, "stdout": stdout, "stderr": stderr},
        summary=f"exit {proc.returncode}: {command[:80]}",
        error=None if proc.returncode == 0 else (stderr.strip() or f"exit code {proc.returncode}"),
    )


# ------------------------------------------------------------------------- git
@tool(
    "git",
    "Run a git subcommand in the repository (status, diff, log, add, commit...).",
    [
        Param("args", "string", "git arguments, e.g. 'status --short'", required=True),
        Param("cwd", "string", "Repository directory", default="."),
    ],
    mutates=True,
    tags=["git"],
)
def git(args: str, cwd: str = ".") -> ToolResult:
    workdir = _resolve(cwd)
    argv = ["git", *re.findall(r"[^\s'\"]+|'[^']*'|\"[^\"]*\"", args)]
    argv = [a.strip("'\"") for a in argv]

    try:
        proc = subprocess.run(argv, cwd=str(workdir), capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise ToolError("git is not installed or not on PATH") from None
    except subprocess.TimeoutExpired:
        raise ToolError("git command timed out after 60s") from None

    return ToolResult(
        ok=proc.returncode == 0,
        data={"exit_code": proc.returncode, "stdout": _truncate(proc.stdout), "stderr": proc.stderr},
        summary=_truncate(proc.stdout or proc.stderr, 400) or f"git {args} -> exit {proc.returncode}",
        error=None if proc.returncode == 0 else proc.stderr.strip(),
    )


# ------------------------------------------------------------------------ todo
_TODO_FILE = ".xli/todos.json"


@tool(
    "todo",
    "Manage the task list: list, add, done, clear.",
    [
        Param(
            "action",
            "string",
            "What to do",
            required=True,
            enum=["list", "add", "done", "clear"],
        ),
        Param("text", "string", "Task text (for add)", default=""),
        Param("index", "integer", "1-based task number (for done)", default=0),
    ],
    mutates=True,
    tags=["plan"],
)
def todo(action: str, text: str = "", index: int = 0) -> ToolResult:
    import json

    path = _resolve(_TODO_FILE)
    items: list[dict[str, Any]] = []
    if path.exists():
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            items = []

    if action == "list":
        return ToolResult.success(
            data=items,
            summary=f"{sum(1 for i in items if not i['done'])} open / {len(items)} total",
        )
    if action == "add":
        if not text.strip():
            raise ToolError("add requires 'text'")
        items.append({"text": text.strip(), "done": False})
    elif action == "done":
        if not 1 <= index <= len(items):
            raise ToolError(f"no task at index {index} (have {len(items)})")
        items[index - 1]["done"] = True
    elif action == "clear":
        items = []
    else:
        raise ToolError(f"unknown action {action!r}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    return ToolResult.success(
        data=items, summary=f"{action}: {sum(1 for i in items if not i['done'])} open"
    )


# ---------------------------------------------------------------------- think
@tool(
    "think",
    "Reason out loud before acting: note a thought without touching anything. "
    "Use it to plan, weigh options or record a conclusion; the thought is shown "
    "to the user as reasoning, never as an answer.",
    [Param("thought", "string", "The thought to record", required=True)],
    tags=["meta"],
)
def think(thought: str) -> ToolResult:
    text = str(thought).strip()
    if not text:
        raise ToolError("think requires a non-empty 'thought'")
    return ToolResult.success(data={"thought": text}, summary=text[:200])


# ------------------------------------------------------------------- delegate
MAX_DELEGATE_DEPTH = 3


@tool(
    "delegate",
    "Delegate a sub-task to a specialised sub-agent. The sub-agent runs with "
    "its own system prompt and toolset, reports its thoughts and actions in the "
    "CLI with its agent ID, and returns a summary when done.",
    [
        Param("agent_name", "string", "Registered sub-agent name (e.g. 'coder', 'researcher')", required=True),
        Param("task", "string", "The task description for the sub-agent", required=True),
        Param("max_steps", "integer", "Max steps for the sub-agent (default 10)", required=False),
    ],
    tags=["meta"],
)
async def delegate(agent_name: str, task: str, max_steps: int = 10) -> ToolResult:
    """Spawn a sub-agent from the registry and run *task* through it."""
    import uuid
    from xli.tools.context import get_agent_context, AgentContext, set_agent_context, reset_agent_context

    ctx = get_agent_context()
    if ctx is None or ctx.provider is None:
        raise ToolError("delegate: no agent context available (not running inside an agent?)")

    if ctx.depth >= MAX_DELEGATE_DEPTH:
        raise ToolError(f"delegate: max nesting depth {MAX_DELEGATE_DEPTH} reached, refusing to prevent infinite loops")

    # Resolve the agent spec
    try:
        from xli.agents.registry import get_registry as get_agent_registry
        registry = get_agent_registry()
        spec = registry.get(agent_name)
    except Exception as exc:
        available = []
        try:
            available = list(registry.list_names())
        except Exception:
            pass
        raise ToolError(
            f"delegate: unknown agent '{agent_name}'. Available: {available}. Error: {exc}"
        )

    # Build a filtered tool registry for the sub-agent
    from xli.tools.registry import ToolRegistry, default_registry
    sub_registry = default_registry(policy=ctx.policy)
    if spec.tools:
        sub_registry = ToolRegistry(policy=ctx.policy)
        full = default_registry(policy=ctx.policy)
        for tname in spec.tools:
            try:
                sub_registry.register(full.get(tname))
            except Exception:
                pass  # skip tools that don't exist

    sub_id = f"{agent_name}-{uuid.uuid4().hex[:8]}"

    # Create the sub-agent
    from xli.agent import Agent
    sub_agent = Agent(
        provider=ctx.provider,
        role=spec.role or agent_name,
        registry=sub_registry,
        policy=ctx.policy,
        max_steps=min(max_steps, 15),
        on_event=ctx.on_event,
        agent_id=sub_id,
        agent_name=agent_name,
    )

    # Set child context so nested delegation works
    child_ctx = AgentContext(
        provider=ctx.provider,
        on_event=ctx.on_event,
        registry=sub_registry,
        policy=ctx.policy,
        parent_agent_id=sub_id,
        depth=ctx.depth + 1,
    )
    token = set_agent_context(child_ctx)
    try:
        result = await sub_agent.run(task)
    finally:
        reset_agent_context(token)

    summary = result.summary or result.text[:500]
    return ToolResult.success(
        data={
            "agent_id": sub_id,
            "agent_name": agent_name,
            "ok": result.ok,
            "steps": len(result.steps),
            "seconds": round(result.seconds, 2),
        },
        summary=f"[{agent_name}:{sub_id}] ok={result.ok} steps={len(result.steps)} — {summary}",
    )


# -------------------------------------------------------------- mcp bridge
# Singleton MCPClient — created lazily so importing builtin never starts
# subprocesses at module level.
_mcp_client: Any = None


def _get_mcp_client() -> Any:
    global _mcp_client
    if _mcp_client is None:
        from xli.mcp.client import MCPClient
        _mcp_client = MCPClient()
    return _mcp_client


@tool(
    "mcp_call",
    "Call a tool on a bundled MCP server (from the mcp/servers/ directory). "
    "Use mcp_list first to discover available servers and tools. "
    "Params should be a JSON object string or a dict of arguments for the MCP tool.",
    [
        Param("server", "string", "MCP server name (e.g. git_mcp, debugger, architecture)", required=True),
        Param("tool_name", "string", "Tool name exposed by that server", required=True),
        Param("params", "string", "JSON object with tool arguments, e.g. '{\"query\": \"foo\"}'", default="{}"),
    ],
    tags=["mcp"],
)
async def mcp_call(server: str, tool_name: str, params: str = "{}") -> ToolResult:
    """Invoke a tool on one of the bundled MCP servers."""
    import json as _json

    # Parse params — accept both a JSON string and a plain dict passed through
    if isinstance(params, str):
        try:
            parsed_params = _json.loads(params) if params.strip() else {}
        except _json.JSONDecodeError as exc:
            raise ToolError(f"params is not valid JSON: {exc}") from exc
    elif isinstance(params, dict):
        parsed_params = params
    else:
        parsed_params = {}

    client = _get_mcp_client()
    try:
        result = await client.call_tool(server, tool_name, parsed_params)
    except Exception as exc:
        raise ToolError(f"MCP call {server}.{tool_name} failed: {exc}") from exc

    summary = f"mcp {server}.{tool_name} -> ok"
    return ToolResult.success(data=result, summary=summary)


@tool(
    "mcp_list",
    "List all bundled MCP servers and their tools. "
    "Pass a server name to see only its tools, or omit to see everything.",
    [
        Param("server", "string", "Optional: show tools for a single server only", default=""),
    ],
    tags=["mcp"],
)
async def mcp_list(server: str = "") -> ToolResult:
    """Discover which MCP servers are available and what tools they expose."""
    from xli.mcp.registry import MCPRegistry

    registry = MCPRegistry()

    if server:
        info = registry.get_server(server)
        if not info:
            raise ToolError(f"unknown MCP server: {server!r}")
        data = {server: info}
    else:
        data = {name: info for name, info in registry.servers.items()}

    enabled = [n for n, i in data.items() if i.get("enabled")]
    summary = f"{len(enabled)} enabled / {len(data)} total MCP servers"
    return ToolResult.success(data=data, summary=summary)


# ----------------------------------------------------------------------- suite
BUILTIN_TOOLS: list[Tool] = [
    read_file,
    write_file,
    edit_file,
    list_dir,
    glob_files,
    grep,
    bash,
    git,
    todo,
    think,
    delegate,
    mcp_call,
    mcp_list,
    *WEB_SEARCH_TOOLS,
    # Charts are how the agent shows numbers: they live here rather than in the
    # UI because the agent has to be able to ask for one mid-task.
    CHART_TOOL,
    # Things the shell does badly: structured edits, symbol outlines, JSON
    # queries and the import graph. See xli/tools/structured.py.
    *STRUCTURED_TOOLS,
]


def builtin_tools() -> list[Tool]:
    """A fresh list of the built-in tools (safe to register more than once)."""
    return list(BUILTIN_TOOLS)
