"""Shared context for tools that need access to the running agent.

Tools are pure functions, but ``delegate`` needs the current provider and
event handler to spawn a sub-agent.  We use :mod:`contextvars` so this is
safe under asyncio concurrency (each task gets its own copy).
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentContext:
    """Everything a tool might need from the running agent."""
    provider: Any = None
    on_event: Any = None
    registry: Any = None
    policy: Any = None
    parent_agent_id: str = "main"
    depth: int = 0  # nesting level to prevent infinite delegation


_ctx: contextvars.ContextVar[AgentContext | None] = contextvars.ContextVar(
    "agent_context", default=None
)


def set_agent_context(ctx: AgentContext) -> contextvars.Token:
    """Install *ctx* for the current async task; returns a token for reset."""
    return _ctx.set(ctx)


def get_agent_context() -> AgentContext | None:
    """Return the current agent context, or ``None`` outside an agent run."""
    return _ctx.get()


def reset_agent_context(token: contextvars.Token) -> None:
    """Restore the previous context."""
    _ctx.reset(token)
