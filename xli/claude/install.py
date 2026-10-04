#!/usr/bin/env python3
"""Claude Code integration — an MCP server, slash commands and a plugin tree.

`xli claude install` is the one command a Claude Code user needs. It wires the
project up in the three places Claude Code actually looks:

    .mcp.json                 MCP server `xli` (project scope)
    .claude/commands/xli.md   /xli <задача>
    CLAUDE.md                 a marked block telling Claude the tools exist

`--user` does the same in the user's own config (`~/.claude.json`,
`~/.claude/commands/`, `~/.claude/CLAUDE.md`) instead of the project, which is
what someone who wants XLI in every project should run once.

Everything is a merge: `install` twice, or with a `.mcp.json` that already
names five other servers, and the file comes out with everyone's entries intact.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from xli.harness import (
    COMMAND_TEMPLATE,
    MCP_COMMAND,
    InstallReport,
    append_block,
    copy_plugin,
    mcp_entry,
    merge_mcp_json,
)

#: Shipped plugin tree: commands + the MCP entry, laid out the way Claude Code
#: expects a plugin to be.
PLUGIN_SOURCE = Path(__file__).resolve().parent / "plugin_root"


def user_config_dir() -> Path:
    """`~/.claude`, which is where user-scope commands and memory live."""
    return Path.home() / ".claude"


def install(
    *,
    project: Path | None = None,
    user: bool = False,
    plugin_dir: Path | None = None,
    command: str = MCP_COMMAND,
    force: bool = False,
) -> InstallReport:
    """Wire XLI into Claude Code. Returns a report; writes nothing else."""
    report = InstallReport()

    if user:
        mcp_path = Path.home() / ".claude.json"
        commands_dir = user_config_dir() / "commands"
        memory = user_config_dir() / "CLAUDE.md"
    else:
        root = Path(project or Path.cwd()).expanduser()
        mcp_path = root / ".mcp.json"
        commands_dir = root / ".claude" / "commands"
        memory = root / "CLAUDE.md"

    changed, why = merge_mcp_json(mcp_path, entry=mcp_entry(command), force=force)
    if changed:
        report.wrote(mcp_path)
    else:
        report.kept(mcp_path, why)

    command_path = commands_dir / "xli.md"
    if command_path.exists() and not force:
        report.kept(command_path, "команда уже есть")
    else:
        command_path.parent.mkdir(parents=True, exist_ok=True)
        command_path.write_text(
            COMMAND_TEMPLATE.format(
                description="Выполнить задачу в этом проекте агентом XLI",
                arguments='"$ARGUMENTS"',
            ),
            encoding="utf-8",
        )
        report.wrote(command_path)

    if memory.exists() or not user:
        changed, why = append_block(memory, CLAUDE_MEMORY_BLOCK_TEXT, force=force)
        if changed:
            report.wrote(memory)
        else:
            report.kept(memory, why)

    if plugin_dir is not None:
        written, skipped, message = copy_plugin(PLUGIN_SOURCE, Path(plugin_dir).expanduser(),
                                                force=force)
        report.written.extend(written)
        report.unchanged.extend(skipped)
        report.messages.append(f"плагин: {message}")

    return report


#: The block appended to CLAUDE.md. Kept here rather than in template syntax:
#: a project memory file is prose, and the markers are what make it removable.
CLAUDE_MEMORY_BLOCK_TEXT = """{start}
## XLI

В этом проекте доступен агент XLI. Его инструменты подключены к сеансу как
MCP-сервер `xli`: чтение и правка файлов, поиск, `bash`, граф зависимостей
(`dep_graph`), карта репозитория (`repo_map`), графики в терминале (`chart`),
суб-агенты (`delegate`).

* `/xli <задача>` — отдать задачу агенту целиком;
* `/xli-graph <модуль>` — граф зависимостей текстом;
* проверить связь: `xli mcp list`, `xli mcp test xli`.

Если сервер запущен с `--with-agent`, доступен и инструмент `xli_agent` —
он выполняет многошаговую задачу сам.
{end}
"""


def plugin_files() -> list[str]:
    """Every file in the shipped plugin tree, relative to its root."""
    if not PLUGIN_SOURCE.exists():
        return []
    return sorted(
        str(path.relative_to(PLUGIN_SOURCE))
        for path in PLUGIN_SOURCE.rglob("*")
        if path.is_file()
    )


def describe() -> dict[str, Any]:
    """What the plugin contains, for `--json` output and the docs."""
    files = plugin_files()
    return {
        "plugin_dir": str(PLUGIN_SOURCE),
        "files": files,
        "commands": [name for name in files if name.startswith("commands/")],
        "mcp": ".mcp.json" in files,
    }


def load_plugin_manifest() -> dict[str, Any]:
    """The plugin manifest, so a test can check it stays valid JSON."""
    return json.loads((PLUGIN_SOURCE / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
