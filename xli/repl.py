#!/usr/bin/env python3
"""
XLI REPL — the interactive line interface.

This is what `xli` with no arguments runs: a prompt, a transcript, and slash
commands. It is intentionally not the TUI — it works over ssh, inside a pipe,
and in terminals curses cannot drive, which is where most agent sessions
actually happen.

Everything the user reads comes from the same two places the CLI and the TUI
read from: `xli.ui.locale.t()` for words and `xli.ui.summary.summarise_call()`
for tool calls, so the three front ends never drift into three dialects.
assistant text is rendered through the markdown parser rather than dumped as
raw source — a heading in the answer looks like a heading here too.
"""

from __future__ import annotations

import argparse
import asyncio
import shlex
from pathlib import Path
from typing import Any

from xli.ui.locale import t
from xli.ui.summary import summarise_call

PROMPT = "❯ "

HELP = """\
commands
  /help                     this text
  /tools                    list the tools the agent can call
  /mode auto|confirm|readonly
  /deny PATTERN             add a deny rule (e.g. /deny '/etc/*')
  /model NAME               switch model
  /session                  show the session id
  /clear                    clear the screen
  /quit                     exit

anything else is sent to the agent as a task.
"""

HELP_RU = """\
команды
  /help                     этот текст
  /tools                    инструменты, доступные агенту
  /mode auto|confirm|readonly   режим прав
  /deny ШАБЛОН              запретить путь (напр. /deny '/etc/*')
  /model ИМЯ                сменить модель
  /session                  показать id сессии
  /clear                    очистить экран
  /quit                     выход

всё остальное уходит агенту как задача.
"""


def _help_text() -> str:
    from xli.ui.locale import lang

    return HELP_RU if lang() == "ru" else HELP


def _print(text: str = "", *, end: str = "\n") -> None:
    print(text, end=end)


class Repl:
    """Line-based front end for the agent."""

    def __init__(self, config, *, registry=None, policy=None, provider=None):
        self.config = config
        self.registry = registry
        self.policy = policy
        self.provider = provider
        self.session = None
        self.allow_all = False
        self.history: list[str] = []

    # ------------------------------------------------------------------ events
    def on_event(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "assistant":
            text = str(payload.get("text", "")).strip()
            if text:
                from xli.ui.ansi import render_markdown_ansi

                _print("\033[35mxli\033[0m")
                _print(render_markdown_ansi(text))
        elif kind == "tool_call":
            name = str(payload.get("name", ""))
            args = payload.get("args") or {}
            if name == "think":
                _print(f"\033[3m  * {str(args.get('thought', '')).strip()}\033[0m")
            else:
                _print(f"\033[2m  -> {name} {summarise_call(name, args)}\033[0m")
        elif kind == "tool_result":
            mark = f"\033[32m{t('ok')}\033[0m" if payload.get("ok") else f"\033[31m{t('fail')}\033[0m"
            _print(f"\033[2m     [{mark}] {str(payload.get('summary', ''))[:140]}\033[0m")
        elif kind == "repair":
            _print(f"\033[33m{t('repaired', detail=payload.get('detail'))}\033[0m")
        elif kind == "warning":
            _print(f"\033[33m{t('warning', message=payload.get('message'))}\033[0m")
        elif kind == "error":
            from xli.ui.locale import humanise_provider_error

            message = humanise_provider_error(str(payload.get("message", "")))
            _print(f"\033[31m{t('error', message=message)}\033[0m")

    # ----------------------------------------------------------------- confirm
    def confirm(self, tool: str, args: dict[str, Any], reason: str) -> bool:
        if self.allow_all:
            return True
        _print(f"\033[33m{t('needs_approval', tool=tool, reason=reason)}\033[0m")
        _print(f"\033[2m{t('args', args=summarise_call(tool, args))}\033[0m")
        yes = ("y", "yes", "д", "да")
        try:
            answer = input(t("repl_allow_prompt")).strip().lower()
        except (EOFError, KeyboardInterrupt, RuntimeError, OSError):
            _print()
            return False
        if answer in ("a", "в"):
            self.allow_all = True
            return True
        return answer in yes

    # ---------------------------------------------------------------- commands
    def slash(self, line: str) -> bool:
        """Handle a slash command. Returns False to exit the REPL."""
        try:
            parts = shlex.split(line[1:])
        except ValueError:
            parts = line[1:].split()
        command = parts[0].lower() if parts else ""
        argument = parts[1] if len(parts) > 1 else ""

        if command == "help":
            _print(_help_text())
        elif command == "tools":
            for name in self.registry.names():
                spec = self.registry.get(name).spec
                flag = t("repl_mutates") if spec.mutates else t("repl_read")
                _print(f"  {name:<10} {flag:<8} {spec.description[:70]}")
        elif command == "mode":
            if argument in ("auto", "confirm", "readonly"):
                from xli.permissions.policy import Mode

                self.policy.mode = Mode.parse(argument)
                self.config.set("permissions.mode", argument)
                _print(t("tui_mode", mode=argument))
            else:
                _print(t("repl_mode_usage"))
        elif command == "deny":
            if argument:
                self.policy.deny.append(argument)
                _print(f"deny += {argument}")
            else:
                _print(t("repl_deny_usage"))
        elif command == "model":
            if argument:
                self.config.set("provider.model", argument)
                _print(t("tui_model", model=argument))
            else:
                _print(t("repl_model_usage"))
        elif command == "session":
            _print(t("tui_session", session=self.session.session_id if self.session else "(none)"))
        elif command == "clear":
            _print("\033[2J\033[H", end="")
        elif command in ("quit", "exit", "q"):
            return False
        else:
            _print(t("tui_unknown_cmd", command=command))
        return True

    # -------------------------------------------------------------------- loop
    async def run_task(self, task: str) -> None:
        from xli.agent import Agent
        from xli.session import Session

        if self.session is None:
            self.session = Session(root=Path.cwd())

        if self.policy is not None:
            self.registry.confirm_handler = self.confirm

        agent = Agent(
            self.provider,
            registry=self.registry,
            policy=self.policy,
            session=self.session,
            max_steps=int(self.config.get("agent.max_steps")),
            temperature=float(self.config.get("provider.temperature")),
            max_tokens=int(self.config.get("provider.max_tokens")),
            on_event=self.on_event,
        )

        try:
            result = await agent.run(task)
            colour = "\033[32m" if result.ok else "\033[31m"
            _print(f"\n{colour}[{result.stopped_reason}]\033[0m {result.summary}\n")
            if result.stopped_reason == "provider_error":
                from xli.ui.locale import humanise_provider_error

                _print("\033[31m  " + humanise_provider_error(result.summary) + "\033[0m")
        except Exception as exc:  # noqa: BLE001 - one bad turn must not end the session
            from xli.ui.locale import humanise_provider_error

            _print(f"\033[31m  {humanise_provider_error(str(exc))}\033[0m")

    def loop(self, initial_task: str = "") -> int:
        from xli.providers.base import get_provider

        if self.provider is None:
            try:
                self.provider = get_provider()
            except ValueError as exc:
                _print(f"\033[31m{t('environment', exc=exc)}\033[0m")
                _print(f"\033[2m{t('env_hint')}\033[0m")
                return 3

        _print(
            f"\033[1mXLI\033[0m \033[2m{self.config.get('provider')}/{self.config.get('provider.model')}"
            f" · {self.config.permission_mode()} · /help\033[0m\n"
        )

        while True:
            try:
                line = input(PROMPT).strip()
            except (EOFError, KeyboardInterrupt, RuntimeError, OSError):
                _print()
                return 0

            if not line:
                continue

            self.history.append(line)

            if line.startswith("/"):
                if not self.slash(line):
                    return 0
                continue

            try:
                asyncio.run(self.run_task(line))
            except KeyboardInterrupt:
                _print(f"\n\033[33m{t('repl_interrupted')}\033[0m")


def run_repl(config, *, registry=None, policy=None, args: argparse.Namespace | None = None) -> int:
    # Repl is defined in this same module, so it is already in scope. The local
    # import that used to be here re-imported xli.repl from inside xli.repl and
    # showed up as a self-cycle in the dependency graph.
    repl = Repl(config, registry=registry, policy=policy)
    initial = " ".join(getattr(args, "task", []) or []) if args else ""
    return repl.loop(initial)
