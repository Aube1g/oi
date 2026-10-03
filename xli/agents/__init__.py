#!/usr/bin/env python3
"""Sub-agents: narrow, delegable specialists defined as data.

    from xli.agents import get_registry

    registry = get_registry(project_root=Path.cwd())
    spec = registry.get("reviewer")
    print(spec.system_prompt())

A sub-agent is a specification, not a process. That is what makes it possible
to list, validate and ship them without executing anything, and it is why
`xli agents verify` can check a spec with no API key.
"""

from __future__ import annotations

from xli.agents.builtin import BUILTIN_NAMES, BUILTIN_SPECS
from xli.agents.registry import AgentRegistry, get_registry, reset_registry
from xli.agents.spec import MODES, NAME_RE, AgentSpec

__all__ = [
    "AgentRegistry",
    "AgentSpec",
    "BUILTIN_NAMES",
    "BUILTIN_SPECS",
    "MODES",
    "NAME_RE",
    "get_registry",
    "reset_registry",
]
