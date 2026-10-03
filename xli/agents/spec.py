#!/usr/bin/env python3
"""Sub-agent definitions.

A sub-agent is a *specification*, not a running process: a name, a role, the
tools it may use, and the limits it runs under. Storing it as data rather than
as code is what makes it possible to list, validate, diff and ship them without
executing anything, and it is why `xli agents verify` can check one without an
API key.

The distinction matters for delegation. The main agent hands a task to a
sub-agent that has a narrower tool set and a stricter permission mode, so a
"reviewer" that can only read cannot write, however confused it gets. That
guarantee comes from the spec, so the spec has to be exact about what it allows.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

#: Sub-agent names appear in file names, tool arguments and log lines, so they
#: are restricted rather than free text.
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")

#: Permission modes a sub-agent may run under. `auto` is deliberately absent:
#: a delegated agent should not be able to widen its own authority.
MODES = ("readonly", "confirm")

#: What a spec is checked against. Anything unknown is an error, not a warning,
#: because a typo in a security-relevant field must not pass silently.
KNOWN_FIELDS = frozenset(
    {
        "name",
        "description",
        "role",
        "tools",
        "mode",
        "model",
        "max_steps",
        "temperature",
        "colour",
        "tags",
        "builtin",
    }
)


@dataclass
class AgentSpec:
    """One sub-agent definition."""

    name: str
    description: str = ""
    role: str = ""
    tools: list[str] = field(default_factory=list)
    mode: str = "confirm"
    model: str = ""
    max_steps: int = 12
    temperature: float = 0.3
    colour: str = "accent"
    tags: list[str] = field(default_factory=list)
    builtin: bool = False

    # ------------------------------------------------------------- conversion
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("builtin", None)   # derived from where it was loaded, not stored
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentSpec:
        """Build a spec, rejecting unknown keys instead of dropping them."""
        unknown = sorted(set(data) - KNOWN_FIELDS)
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(unknown)}")
        known = {key: value for key, value in data.items() if key in KNOWN_FIELDS - {"builtin"}}
        if "tools" in known and isinstance(known["tools"], str):
            known["tools"] = [t.strip() for t in known["tools"].split(",") if t.strip()]
        if "tags" in known and isinstance(known["tags"], str):
            known["tags"] = [t.strip() for t in known["tags"].split(",") if t.strip()]
        return cls(**known)

    # ------------------------------------------------------------- validation
    def validate(self, available_tools: list[str] | None = None) -> list[str]:
        """Return a list of problems. An empty list means the spec is usable.

        `available_tools` lets the caller catch a spec that names a tool which
        no longer exists, which is the failure that otherwise shows up much
        later as a delegation that silently does nothing.
        """
        problems: list[str] = []

        if not NAME_RE.match(self.name or ""):
            problems.append(
                f"name {self.name!r} must match {NAME_RE.pattern} "
                "(lowercase, 2-40 chars, starting with a letter)"
            )
        if not self.description.strip():
            problems.append("description is empty; the main agent uses it to decide when to delegate")
        if not self.role.strip():
            problems.append("role is empty; the sub-agent would run with no instructions")
        if self.mode not in MODES:
            problems.append(f"mode {self.mode!r} must be one of {', '.join(MODES)}")
        if not 1 <= int(self.max_steps) <= 100:
            problems.append(f"max_steps {self.max_steps} must be between 1 and 100")
        if not 0.0 <= float(self.temperature) <= 2.0:
            problems.append(f"temperature {self.temperature} must be between 0.0 and 2.0")
        if not self.tools:
            problems.append("tools is empty; the sub-agent could not act on anything")

        if available_tools is not None:
            missing = [tool for tool in self.tools if tool not in available_tools]
            if missing:
                problems.append(f"unknown tool(s): {', '.join(sorted(missing))}")

        return problems

    # ----------------------------------------------------------------- prompts
    def system_prompt(self, *, project: str = "", extra: str = "") -> str:
        """The prompt this sub-agent runs with.

        Deliberately narrower than the main agent's: it states the role, the
        boundary of what it may touch, and nothing else, because a delegated
        agent that thinks it owns the whole project is how a reviewer ends up
        rewriting the thing it was asked to read.
        """
        lines = [
            f"You are {self.name}, a focused sub-agent.",
            "",
            self.role.strip(),
            "",
            "BOUNDARIES:",
            f"- You may use only these tools: {', '.join(self.tools) or 'none'}.",
            f"- Permission mode is {self.mode}; ask rather than assume.",
            f"- You have at most {self.max_steps} steps. Finish or report why you cannot.",
            "- Report findings; do not widen the task you were given.",
        ]
        if project:
            lines += ["", f"Project root: {project}"]
        if extra.strip():
            lines += ["", extra.strip()]
        return "\n".join(lines)
