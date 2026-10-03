#!/usr/bin/env python3
"""
XLI Kernel methods — the RPC surface frontends drive.

This is the contract a Go (or any other) client codes against. Everything the
Python CLI can do is reachable here over JSON-RPC, so a foreign frontend is a
presentation layer, not a reimplementation.

Method map
----------
    hello                      protocol handshake
    agent.run      {task}      run a task to completion; streams agent.* events
    agent.tools                the tool catalogue as JSON schema
    tools.list / tools.run     inspect and invoke tools directly
    config.list / config.get / config.set
    kernel.status / kernel.build / kernel.clean / kernel.preflight
    session.list / session.load
    doctor                     environment report

Notifications the server emits while `agent.run` is in flight:

    agent.start, step, assistant, tool_call, tool_result, repair, warning,
    error, agent.end
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from xli.kernel.protocol import PROTOCOL_VERSION
from xli.kernel.server import KernelServer


def build_kernel(
    *,
    project_root: Path | None = None,
    provider: Any = None,
    policy: Any = None,
) -> KernelServer:
    """Assemble a kernel with the full method set.

    `provider` and `policy` are injectable so tests (and an embedder that
    already has them) need neither an API key nor a writable config on disk.
    """
    from xli.manager.config import Config
    from xli.permissions.policy import Mode, Policy
    from xli.tools.registry import default_registry

    server = KernelServer()
    root = Path(project_root) if project_root else Path.cwd()
    config = Config.load(project_root=root)
    if policy is None:
        policy = Policy(mode=Mode.parse(config.permission_mode()), root=root)
    registry = default_registry(policy=policy)

    state: dict[str, Any] = {"provider": provider, "config": config}

    # ------------------------------------------------------------------ hello
    @server.method("hello", required=("protocol_version",))
    def hello(protocol_version: int, client: str = "unknown", capabilities: list[str] = None):
        compatible = int(protocol_version) == PROTOCOL_VERSION
        server.client = {
            "protocol_version": int(protocol_version),
            "client": client,
            "capabilities": capabilities or [],
        }
        return {
            "compatible": compatible,
            "protocol_version": PROTOCOL_VERSION,
            "implementation": "xli-kernel",
            "version": _version(),
            "methods": len(server.list_methods()),
        }

    # ------------------------------------------------------------------ agent
    @server.method("agent.run", required=("task",), doc="Run a task to completion.")
    async def agent_run(task: str, max_steps: int | None = None, mode: str | None = None):
        from xli.agent import Agent

        active_policy = policy
        if mode:
            active_policy = Policy(mode=Mode.parse(mode), root=root)
            registry.policy = active_policy

        provider = _resolve_provider(state)

        async def relay(kind: str, payload: dict[str, Any]) -> None:
            await server.notify(f"agent.{kind}", payload)

        agent = Agent(
            provider,
            registry=registry,
            policy=active_policy,
            max_steps=int(max_steps or config.get("agent.max_steps")),
            on_event=lambda kind, payload: _schedule(relay(kind, payload)),
        )
        result = await agent.run(task)
        return result.to_dict()

    @server.method("agent.tools", doc="Tool catalogue as JSON schema.")
    def agent_tools():
        return {"tools": registry.schema(), "prompt": registry.prompt_block()}

    # ------------------------------------------------------------------ tools
    @server.method("tools.list", doc="Every registered tool.")
    def tools_list():
        return {"tools": registry.schema(), "stats": registry.stats()}

    @server.method("tools.run", required=("name",), doc="Invoke one tool.")
    async def tools_run(name: str, args: dict[str, Any] | None = None):
        result = await registry.execute(name, args or {})
        return result.to_dict()

    # ----------------------------------------------------------------- config
    @server.method("config.list", doc="All settings.")
    def config_list():
        return {"config": dict(state["config"].items())}

    @server.method("config.get", required=("key",), doc="Read one setting.")
    def config_get(key: str):
        return {"key": key, "value": state["config"].get(key)}

    @server.method("config.set", required=("key", "value"), doc="Write one setting.")
    def config_set(key: str, value: Any):
        coerced = state["config"].set(key, value)
        path = state["config"].save()
        return {"key": key, "value": coerced, "path": str(path)}

    # ----------------------------------------------------------------- kernel
    @server.method("kernel.preflight", doc="Toolchain readiness.")
    def kernel_preflight():
        from xli.manager import kernel_build

        return kernel_build.preflight().to_dict()

    @server.method("kernel.status", doc="What is compiled.")
    def kernel_status():
        from xli.manager import kernel_build

        return kernel_build.status()

    @server.method("kernel.build", doc="Compile xli.core with Cython.")
    def kernel_build(targets: list[str] | None = None, force: bool = False):
        from xli.manager import kernel_build as kb

        return kb.build(targets, force=force).to_dict()

    @server.method("kernel.clean", doc="Remove build artefacts.")
    def kernel_clean():
        from xli.manager import kernel_build

        return kernel_build.clean()

    # ---------------------------------------------------------------- session
    @server.method("session.list", doc="Stored sessions, newest first.")
    def session_list():
        from xli.session import Session

        return {"sessions": Session.list_sessions(root=root)}

    @server.method("session.load", required=("id",), doc="Replay one session.")
    def session_load(id: str):
        from xli.session import Session

        session = Session.latest(root=root) if id == "--latest" else Session.load(id, root=root)
        if session is None:
            return {"found": False}
        return {
            "found": True,
            "id": session.session_id,
            "events": [e.to_dict() for e in session.events],
            "transcript": session.transcript(),
        }

    # ---------------------------------------------------------------- plugins
    @server.method("plugins.list", doc="Installed XPI plugins.")
    def plugins_list():
        from xli.xpi.manager import XpiManager

        manager = state.setdefault("plugins", XpiManager())
        return {"plugins": manager.list_plugins(), "active": manager.registry.list_active()}

    @server.method("plugins.reload", required=("name",), doc="Hot-reload one plugin.")
    def plugins_reload(name: str):
        from xli.xpi.manager import XpiManager

        manager = state.setdefault("plugins", XpiManager())
        return {"name": name, "reloaded": manager.reload(name)}

    @server.method(
        "plugins.set_enabled", required=("name", "enabled"), doc="Enable or disable a plugin."
    )
    def plugins_set_enabled(name: str, enabled: bool):
        from xli.xpi.manager import XpiManager

        manager = state.setdefault("plugins", XpiManager())
        return {"name": name, "ok": manager.set_enabled(name, bool(enabled))}

    @server.method("plugins.state", doc="Shared XPI state across frontends.")
    def plugins_state():
        from xli.xpi.state import XpiState

        return {"state": XpiState().all()}

    @server.method("plugins.dispatch", required=("hook",), doc="Fire an XPI hook now.")
    def plugins_dispatch(hook: str, context: dict[str, Any] | None = None):
        from xli.xpi.manager import XpiManager

        manager = state.setdefault("plugins", XpiManager())
        return manager.dispatch(hook, **(context or {})).to_dict()

    # ---------------------------------------------------------------- context
    @server.method("context.scout", doc="Scan the project and report its shape.")
    def context_scout(path: str | None = None):
        from dataclasses import asdict

        from xli.core.context_scout import ContextScout

        scout = ContextScout(path or str(root))
        ctx = scout.scan()
        return {
            "name": ctx.name,
            "language": ctx.language,
            "framework": ctx.framework,
            "key_files": ctx.key_files,
            "dependencies": ctx.dependencies,
            "conventions": ctx.conventions,
            "patterns": [asdict(pat) for pat in ctx.patterns],
        }

    @server.method(
        "context.agents_md", required=(), doc="Generate (and optionally write) AGENTS.md."
    )
    def context_agents_md(path: str | None = None, save: bool = False):
        from xli.core.context_scout import ContextScout

        scout = ContextScout(path or str(root))
        if save:
            written = scout.save_agents_md()
            return {"saved": True, "path": str(written)}
        return {"saved": False, "content": scout.generate_agents_md()}

    # ------------------------------------------------------------------ inbox
    def _inbox(project: str | None, team: str | None):
        from xli.core.inbox import TeamInbox

        key = (project or "default", team or "default")
        existing = state.get("inboxes", {}).get(key)
        if existing is None:
            existing = TeamInbox(project=key[0], team=key[1])
            state.setdefault("inboxes", {})[key] = existing
        return existing

    @server.method("inbox.send", required=("sender", "recipient", "text"), doc="Message an agent.")
    async def inbox_send(sender: str, recipient: str, text: str,
                         project: str | None = None, team: str | None = None):
        msg = await _inbox(project, team).send(sender, recipient, text)
        return {"message": msg.to_dict()}

    @server.method("inbox.read", required=("agent",), doc="Read an agent's inbox.")
    def inbox_read(agent: str, since: str | None = None, limit: int = 50,
                   project: str | None = None, team: str | None = None):
        msgs = _inbox(project, team).read_messages(agent, since=since, limit=limit)
        return {"agent": agent, "count": len(msgs), "messages": [m.to_dict() for m in msgs]}

    @server.method("inbox.broadcast", required=("sender", "text"), doc="Message every agent.")
    async def inbox_broadcast(sender: str, text: str, exclude: list[str] | None = None,
                              project: str | None = None, team: str | None = None):
        sent = await _inbox(project, team).broadcast(sender, text, exclude=exclude or [])
        return {"sent": [m.to_dict() for m in (sent or [])]}

    @server.method("inbox.status", doc="Inbox directory and per-agent message counts.")
    def inbox_status(project: str | None = None, team: str | None = None):
        box = _inbox(project, team)
        agents = sorted(
            f.stem for f in box.base_dir.glob("*.jsonl") if f.is_file()
        ) if box.base_dir.is_dir() else []
        return {
            "project": box.project,
            "team": box.team,
            "dir": str(box.base_dir),
            "agents": [
                {"agent": a, "messages": len(box.read_messages(a))} for a in agents
            ],
        }

    # ------------------------------------------------------------------- heal
    @server.method("heal.analyze", required=("error",), doc="Classify an error as retryable.")
    async def heal_analyze(error: str, context: str = ""):
        from xli.core.self_healing import get_healing_engine

        analysis = await get_healing_engine().analyze_error(
            RuntimeError(error), context
        )
        return analysis.to_dict()

    @server.method("heal.history", doc="Errors seen by the healing engine.")
    def heal_history():
        from xli.core.self_healing import get_healing_engine

        return {"history": get_healing_engine().error_history}

    # ------------------------------------------------------------ recommend
    @server.method(
        "recommend.for_task", required=("task",), doc="Suggest MCP servers and skills for a task."
    )
    def recommend_for_task(task: str, agent: str = "coder"):
        from xli.mcp.recommender import get_recommender

        return get_recommender().recommend_for_task(task)

    @server.method(
        "recommend.context", required=("task",), doc="Full recommended context block for a task."
    )
    def recommend_context(task: str, agent: str = "coder"):
        from xli.mcp.recommender import get_recommender

        return {"context": get_recommender().build_full_context(task, agent)}

    @server.method("mcp.servers", doc="All known MCP servers and whether they are enabled.")
    def mcp_servers():
        from xli.mcp.registry import MCPRegistry

        return {"servers": MCPRegistry().list_all()}

    @server.method("mcp.set_enabled", required=("name", "enabled"), doc="Enable or disable a server.")
    def mcp_set_enabled(name: str, enabled: bool):
        from xli.mcp.recommender import reset_recommender
        from xli.mcp.registry import MCPRegistry

        registry = MCPRegistry()
        registry.enable(name) if enabled else registry.disable(name)
        # The recommender caches the registry, so it must be rebuilt.
        reset_recommender()
        info = registry.get_server(name)
        return {"name": name, "enabled": bool(info and info.get("enabled"))}

    # --------------------------------------------------------------- snapshot
    @server.method("snapshot.list", doc="File snapshots, newest first.")
    def snapshot_list():
        from xli.core.time_machine import get_time_machine

        return {"snapshots": get_time_machine().list_snapshots()}

    @server.method(
        "snapshot.create",
        required=("paths",),
        doc="Snapshot files before editing them.",
    )
    def snapshot_create(paths: list[str], label: str | None = None):
        from xli.core.time_machine import get_time_machine

        if isinstance(paths, str):
            paths = [paths]
        if not paths:
            raise ValueError("paths must not be empty")
        return {
            "id": get_time_machine().snapshot([str(p) for p in paths], label),
            "files": len(paths),
        }

    @server.method("snapshot.rollback", required=("id",), doc="Restore files from a snapshot.")
    def snapshot_rollback(id: str):
        from xli.core.time_machine import get_time_machine

        if not get_time_machine().rollback(id):
            raise ValueError(f"no such snapshot: {id}")
        return {"rolled_back": id}

    @server.method(
        "snapshot.diff", required=("id", "path"), doc="Diff one file against a snapshot."
    )
    def snapshot_diff(id: str, path: str):
        from xli.core.time_machine import get_time_machine

        return {"id": id, "path": path, "diff": get_time_machine().diff_snapshot(id, str(path))}

    @server.method("snapshot.delete", required=("id",), doc="Discard a snapshot.")
    def snapshot_delete(id: str):
        from xli.core.time_machine import get_time_machine

        if not get_time_machine().delete_snapshot(id):
            raise ValueError(f"no such snapshot: {id}")
        return {"deleted": id}

    # ----------------------------------------------------------------- doctor
    @server.method("doctor", doc="Environment report.")
    def doctor():
        from xli.manager import kernel_build

        checks: list[dict[str, Any]] = [{"name": "python", "ok": True, "detail": _python()}]
        for module in ("httpx", "rich", "Cython", "textual", "pynvim"):
            try:
                __import__(module)
                checks.append({"name": module, "ok": True, "detail": "installed"})
            except ImportError:
                checks.append({"name": module, "ok": False, "detail": "not installed"})
        for check in kernel_build.preflight().checks:
            checks.append(
                {"name": f"kernel.{check.name}", "ok": check.ok, "detail": check.detail}
            )
        return {"checks": checks, "ok": all(c["ok"] for c in checks)}

    return server


# --------------------------------------------------------------------- helpers
def _resolve_provider(state: dict[str, Any]) -> Any:
    """Use the injected provider, or build the configured one on first need."""
    if state["provider"] is None:
        from xli.providers.base import get_provider

        state["provider"] = get_provider()
    return state["provider"]


def _schedule(coro) -> None:
    """Fire a notification coroutine from a sync callback."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    loop.create_task(coro)


def _version() -> str:
    try:
        from xli import VERSION

        return VERSION
    except ImportError:  # pragma: no cover - cli always present in practice
        return "unknown"


def _python() -> str:
    import sys

    return sys.version.split()[0]
