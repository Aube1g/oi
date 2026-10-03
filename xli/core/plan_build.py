#!/usr/bin/env python3
"""
XLI Plan/Build Mode Switcher
Like OpenCode: Plan = read-only analysis, Build = can modify files
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xli.permissions.policy import Mode as PolicyMode

from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.mode")


class Mode(Enum):
    PLAN = "plan"
    BUILD = "build"


@dataclass
class ModeContext:
    """Context for current mode"""
    mode: Mode
    system_message: str
    allowed_tools: list
    can_modify_files: bool
    can_run_shell: bool


#: Tools available in plan mode, by real registry name. Curated by hand: the
#: plan posture promises "nothing that changes the machine", so this list must
#: stay a small, auditable subset.
#:
#: These used to be ["read", "browse"] and ["read", "write", "edit", "bash",
#: "task"]. Neither "browse" nor "task" exists in xli.tools.registry, so
#: can_use_tool() denied ls, glob, grep, git and todo in plan mode and ls, glob,
#: grep, git and todo in build mode. tests/test_plan_build.py pins every name
#: here against the live registry so this cannot drift again.
PLAN_TOOLS: list[str] = ["read", "ls", "glob", "grep", "todo", "think"]

#: Build mode may use everything that exists. Written out rather than derived
#: from the registry: deriving it meant importing the tool modules from
#: plan_build, which added another edge to the very import cycle this file is
#: in the middle of. The literal is pinned against the live registry by
#: tests/test_plan_build.py::TestAllowedToolsAreReal, so a new tool that is
#: forgotten here fails the suite instead of being silently denied at runtime.
BUILD_TOOLS: list[str] = [
    "read", "ls", "glob", "grep", "todo", "think",
    "write", "edit", "bash", "git",
    "web_search", "fetch_page", "fetch_pages", "search_status",
    "mcp_call", "mcp_list",
    "apply_patch", "outline", "json_query", "repo_map", "dep_graph",
]


MODE_CONFIGS = {
    Mode.PLAN: ModeContext(
        mode=Mode.PLAN,
        system_message="""You are in PLAN mode. You are an expert software architect.

Your job is to analyze, plan, and design solutions. You CANNOT:
- Create or modify files
- Run shell commands
- Install dependencies

You CAN:
- Read files to understand the codebase
- Analyze code structure and patterns
- Create detailed plans and designs
- Suggest improvements and optimizations
- Write pseudocode and examples

Output your analysis as structured text with clear sections.
""",
        allowed_tools=PLAN_TOOLS,
        can_modify_files=False,
        can_run_shell=False
    ),

    Mode.BUILD: ModeContext(
        mode=Mode.BUILD,
        system_message="""You are in BUILD mode. You are an expert developer.

Your operational mode changed from plan to build. You are permitted to make file changes.

You CAN:
- Create new files
- Edit existing files
- Run shell commands (safely)
- Install dependencies (if needed)
- Run tests
- Execute code

You MUST:
- Follow the plan created in PLAN mode
- Make incremental changes
- Test your changes
- Handle errors gracefully

Use tools to accomplish tasks. Think step by step.
""",
        allowed_tools=BUILD_TOOLS,
        can_modify_files=True,
        can_run_shell=True
    )
}


class ModeSwitcher:
    """Handles Plan/Build mode switching"""

    def __init__(self, initial_mode: Mode = Mode.PLAN):
        self.current_mode = initial_mode
        self.mode_history = [initial_mode]
        self.logger = StructuredLogger("xli.mode")

    def switch(self, new_mode: Mode) -> ModeContext:
        """Switch to new mode"""
        old_mode = self.current_mode
        self.current_mode = new_mode
        self.mode_history.append(new_mode)

        config = MODE_CONFIGS[new_mode]

        self.logger.log_structured("INFO", "mode",
                                  f"Switched from {old_mode.value} to {new_mode.value}")

        return config

    def toggle(self) -> ModeContext:
        """Toggle between Plan and Build"""
        new_mode = Mode.BUILD if self.current_mode == Mode.PLAN else Mode.PLAN
        return self.switch(new_mode)

    def get_context(self) -> ModeContext:
        """Get current mode context"""
        return MODE_CONFIGS[self.current_mode]

    def is_plan(self) -> bool:
        return self.current_mode == Mode.PLAN

    def is_build(self) -> bool:
        return self.current_mode == Mode.BUILD

    def can_use_tool(self, tool_name: str) -> bool:
        """Check if tool is allowed in current mode"""
        config = self.get_context()
        return tool_name in config.allowed_tools

    def get_system_prompt(self) -> str:
        """Get system prompt for current mode"""
        return self.get_context().system_message

    def get_status_line(self) -> str:
        """Get status line for UI"""
        mode = self.current_mode.value.upper()
        icon = "📋" if self.is_plan() else "🔨"
        tools = ", ".join(self.get_context().allowed_tools)
        return f"{icon} MODE: {mode} | Tools: {tools}"


# Global mode switcher instance
_mode_switcher: ModeSwitcher | None = None


def get_mode_switcher() -> ModeSwitcher:
    """Get global mode switcher"""
    global _mode_switcher
    if _mode_switcher is None:
        _mode_switcher = ModeSwitcher(Mode.PLAN)
    return _mode_switcher


def set_mode_switcher(switcher: ModeSwitcher):
    """Set global mode switcher"""
    global _mode_switcher
    _mode_switcher = switcher


# --------------------------------------------------------------- permissions
def reset_mode_switcher() -> None:
    """Drop the singleton. Tests need this; nothing else should."""
    global _mode_switcher
    _mode_switcher = None


def permission_mode(mode: Mode) -> PolicyMode:
    """Map a plan/build mode onto the permissions policy.

    Plan mode is read-only in substance, so it maps to Policy mode READONLY —
    the enforcement lives in xli.permissions.policy, not in can_use_tool().
    Having two independent gates that disagree is how a tool call ends up
    allowed by one and blocked by the other. Build mode keeps the caller's
    existing posture, defaulting to CONFIRM.
    """
    from xli.permissions.policy import Mode as PolicyMode

    if mode is Mode.PLAN:
        return PolicyMode.READONLY
    return PolicyMode.CONFIRM


def policy_for(mode: Mode, *, allow: list[str] | None = None, root=None):
    """Build a Policy enforcing `mode`. Convenience for callers and the kernel."""
    from xli.permissions.policy import Policy

    return Policy(mode=permission_mode(mode), allow=list(allow or []), root=root)


__all__ = [
    "BUILD_TOOLS",
    "MODE_CONFIGS",
    "PLAN_TOOLS",
    "Mode",
    "ModeContext",
    "ModeSwitcher",
    "get_mode_switcher",
    "permission_mode",
    "policy_for",
    "reset_mode_switcher",
    "set_mode_switcher",
]
