#!/usr/bin/env python3
"""Install XLI into other agents' homes: Claude Code and Codex.

Both harnesses let a user point at an MCP server and read a few markdown files
for instructions. That is the whole integration surface, and it is enough to
make XLI useful from inside them:

* **Claude Code** reads `.mcp.json` (project) or `~/.claude.json` (user) for
  MCP servers, `.claude/commands/*.md` (or `~/.claude/commands/`) for slash
  commands, `CLAUDE.md` for project memory, and `.claude-plugin/plugin.json`
  for a plugin package.
* **Codex** reads `~/.codex/config.toml` with `[mcp_servers.<name>]`, and
  `~/.codex/prompts/*.md` for custom prompts.

Two rules run through all of it, because both files are files a user already
owns:

1. **merge, never clobber.** A `.mcp.json` that already names three servers,
   or a `config.toml` full of the user's own settings, must come out the other
   side with those intact. We only add our entry and only when it is missing
   (or when `force` says to replace our own previous entry).
2. **say what changed.** Every installer returns a report of what it wrote,
   what it skipped and why, and the CLI prints it in Russian. Silently editing
   someone's editor configuration is the kind of thing that costs trust.

Nothing here needs the harness to be installed: the files are ordinary text,
and the tests write them into a temporary directory.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The command other harnesses should run to get XLI's tools. `xli` is the
#: console script; users who run from a checkout can override it.
MCP_COMMAND = "xli"
MCP_ARGS = ["mcp", "serve"]

#: Marker for the block we append to CLAUDE.md / AGENTS.md. Removing the
#: integration means deleting everything between the markers.
MARK_START = "<!-- xli:start -->"
MARK_END = "<!-- xli:end -->"

#: The block Codex users get in a project's AGENTS.md, when they ask for it.
AGENTS_BLOCK_TEXT = """{start}
## XLI

В проекте подключён MCP-сервер `xli` (секция `[mcp_servers.xli]` в
`~/.codex/config.toml`): правка файлов, поиск, `bash`, граф зависимостей,
карта репозитория, графики в терминале, суб-агенты.

Задача целиком: `codex` может вызвать инструмент `xli_agent` (если сервер
запущен как `xli mcp serve --with-agent`), либо выполнить `xli run "<задача>"`.
{end}
"""

COMMAND_TEMPLATE = """---
description: {description}
allowed-tools: Bash(xli:*)
---

Запусти XLI на задаче и покажи результат:

```bash
xli run {arguments} --json
```

Затем разбери его вывод: что агент изменил, какие инструменты звал, что осталось
сделать. Не пересказывай JSON дословно — сформулируй по-русски.
"""

CODEX_PROMPT = """# XLI

Проси XLI выполнить задачу в этом проекте:

```bash
xli run $ARGUMENTS
```

У XLI есть свои инструменты (правка файлов, поиск, граф зависимостей, графики
в терминале, суб-агенты). MCP-сервер `xli` (секция `[mcp_servers.xli]` в
`~/.codex/config.toml`) отдаёт их этому сеансу.
"""


@dataclass
class InstallReport:
    """What an installer did, in the order it did it."""

    ok: bool = True
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    def wrote(self, path: Path) -> None:
        self.written.append(str(path))

    def kept(self, path: Path, why: str = "уже настроено") -> None:
        self.unchanged.append(str(path))
        self.messages.append(f"{path}: {why}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "written": list(self.written),
            "unchanged": list(self.unchanged),
            "messages": list(self.messages),
        }


def mcp_entry(command: str = MCP_COMMAND, args: list[str] | None = None) -> dict[str, Any]:
    return {"command": command, "args": list(args or MCP_ARGS)}


def merge_mcp_json(path: Path, *, entry: dict[str, Any], force: bool = False) -> tuple[bool, str]:
    """Add our server to a `.mcp.json`-shaped file. Returns (changed, reason).

    An entry we put there before is replaced only when `force` is set: a user
    who edited the command by hand (a venv path, `python -m xli`) should not
    have that edit undone by the next install.
    """
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError as exc:
            return False, f"файл не разобрать как JSON ({exc}); не трогаю"
        if not isinstance(data, dict):
            return False, "в файле не объект JSON; не трогаю"

    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return False, "поле mcpServers занято не объектом; не трогаю"

    existing = servers.get("xli")
    if existing is not None and not force:
        same = existing == entry
        return False, "сервер xli уже описан" + (" так же" if same else " иначе (--force заменит)")

    servers["xli"] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True, "добавлен сервер xli"


def append_block(path: Path, block: str, *, force: bool = False) -> tuple[bool, str]:
    """Append a marked block to a markdown file, once."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if MARK_START in text:
        if not force:
            return False, "раздел уже есть"
        start = text.index(MARK_START)
        end = text.index(MARK_END, start) + len(MARK_END)
        text = text[:start] + text[end:].lstrip("\n")
        text = text.rstrip() + "\n\n" if text.strip() else ""

    addition = block.format(start=MARK_START, end=MARK_END)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((text.rstrip() + "\n\n" + addition).lstrip("\n"), encoding="utf-8")
    return True, "раздел добавлен"


def install_codex_mcp(config: Path, *, command: str = MCP_COMMAND,
                      args: list[str] | None = None, force: bool = False) -> tuple[bool, str]:
    """Add `[mcp_servers.xli]` to a Codex `config.toml`.

    Codex keeps its MCP servers in TOML. We do not rewrite the file through a
    TOML library: comments, key order and formatting are the user's, and a
    round-trip through a serialiser would quietly discard them. Appending a
    section that is not already there is the one edit that provably cannot
    disturb the rest of the file.
    """
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    if "[mcp_servers.xli]" in text:
        if not force:
            return False, "секция [mcp_servers.xli] уже есть"
        lines: list[str] = []
        skipping = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped == "[mcp_servers.xli]":
                skipping = True
                continue
            if skipping and stripped.startswith("[") and stripped.endswith("]"):
                skipping = False
            if not skipping:
                lines.append(line)
        text = "\n".join(lines).rstrip() + "\n"

    args_toml = ", ".join(json.dumps(arg) for arg in (args or MCP_ARGS))
    section = (
        "\n[mcp_servers.xli]\n"
        f"command = {json.dumps(command)}\n"
        f"args = [{args_toml}]\n"
        "startup_timeout_sec = 20\n"
        "tool_timeout_sec = 120\n"
    )
    config.parent.mkdir(parents=True, exist_ok=True)
    body = (text.rstrip() + "\n" + section).lstrip("\n")
    config.write_text(body, encoding="utf-8")
    return True, "добавлен [mcp_servers.xli]"


def copy_plugin(source: Path, target: Path, *, force: bool = False) -> tuple[list[str], list[str], str]:
    """Copy a plugin tree. Returns (written, skipped, message)."""
    if not source.exists():
        return [], [], f"нет каталога плагина: {source}"
    written: list[str] = []
    skipped: list[str] = []
    for item in sorted(source.rglob("*")):
        if item.is_dir():
            continue
        relative = item.relative_to(source)
        destination = target / relative
        if destination.exists() and not force:
            skipped.append(str(destination))
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)
        written.append(str(destination))
    if not written and skipped:
        return written, skipped, "всё уже на месте (--force перезапишет)"
    return written, skipped, f"установлено файлов: {len(written)}"
