#!/usr/bin/env python3
"""Codex integration — the MCP server and a custom prompt.

Codex reads two files that matter here:

    ~/.codex/config.toml            [mcp_servers.<name>] tables
    ~/.codex/prompts/*.md           custom prompts, $ARGUMENTS substituted

`xli codex install` adds both. The config edit is an append, deliberately: the
file is a user's, full of their own settings and comments, and rewriting it
through a TOML round-trip would discard the comments. Appending a section that
is not already present cannot disturb anything above it.

The project side (`AGENTS.md`) is only touched when a project directory is
named explicitly, because `xli codex install` run from inside a random
repository should not start editing that repository's instructions.
"""

from __future__ import annotations

from pathlib import Path

from xli.harness import (
    AGENTS_BLOCK_TEXT,
    CODEX_PROMPT,
    MCP_COMMAND,
    InstallReport,
    append_block,
    install_codex_mcp,
)

PROMPT_NAME = "xli.md"


def codex_home() -> Path:
    """`$CODEX_HOME` when set, otherwise `~/.codex` — Codex's own rule."""
    import os

    override = os.environ.get("CODEX_HOME")
    return Path(override).expanduser() if override else Path.home() / ".codex"


def install(
    *,
    home: Path | None = None,
    project: Path | None = None,
    command: str = MCP_COMMAND,
    force: bool = False,
) -> InstallReport:
    """Wire XLI into Codex. Returns a report; writes nothing else."""
    report = InstallReport()
    base = Path(home).expanduser() if home else codex_home()

    config = base / "config.toml"
    changed, why = install_codex_mcp(config, command=command, force=force)
    if changed:
        report.wrote(config)
    else:
        report.kept(config, why)

    prompt = base / "prompts" / PROMPT_NAME
    if prompt.exists() and not force:
        report.kept(prompt, "промпт уже есть")
    else:
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text(CODEX_PROMPT, encoding="utf-8")
        report.wrote(prompt)

    if project is not None:
        agents = Path(project).expanduser() / "AGENTS.md"
        changed, why = append_block(agents, AGENTS_BLOCK_TEXT, force=force)
        if changed:
            report.wrote(agents)
        else:
            report.kept(agents, why)

    return report
