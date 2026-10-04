#!/usr/bin/env python3
"""
XLI Agent — the loop that turns a task into finished work.

One turn looks like:

    build prompt -> ask the model -> parse tool calls -> run them
                 -> append results -> repeat

until the model emits `<done>` or the step budget runs out.

Design rules this file follows:

  * **No I/O of its own.** Every visible thing goes through `on_event`, so the
    CLI, the TUI and the Neovim bridge render the same events differently.
  * **A tool failure is data, not an exception.** The model is told what went
    wrong and gets another turn — that is usually enough for it to correct
    course, and it is the single biggest reliability win over letting the loop
    die.
  * **The parser's repairs are fed back.** If a call had to be repaired to be
    readable, the model hears about it, which measurably reduces repeats.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from collections.abc import Callable

import re

from xli.parse import ParsedResponse, ToolCall, parse_response
from xli.permissions.policy import Mode, Policy

if TYPE_CHECKING:
    from xli.core.plan_build import Mode as WorkMode
from xli.session import Session
from xli.tools.base import ToolResult
from xli.tools.registry import ToolRegistry, default_registry

EVENT_AGENT = "agent"

#: How many times one turn may be re-asked after an unparseable call. Three is
#: enough to fix a malformed tag; a fourth means the model cannot produce one,
#: and burning the step budget on it would be worse than reporting the failure.
MAX_PARSE_RECOVERIES = 3

#: Signature of the progress callback frontends supply.
EventHandler = Callable[[str, dict[str, Any]], Any]

SYSTEM_PROMPT_TEMPLATE = """You are XLI, an autonomous coding agent working in a real repository.

You act by calling tools. Reply with prose for the user, and call tools with:
<tool>{{"name": "<tool>", "args": {{...}}}}</tool>

When the task is complete, end your reply with <done>short summary</done>.

Rules:
- Read before you edit. Never guess at file contents.
- Make the smallest change that fixes the problem.
- After changing code, verify it: run the tests or the command that proves it works.
- If a tool returns an error, read it and correct your approach. Do not repeat an identical call.
- Do not claim something is done unless a tool result shows it.

Available tools:
{tools}
"""


@dataclass
class StepOutcome:
    index: int
    text: str
    calls: list[ToolCall] = field(default_factory=list)
    results: list[ToolResult] = field(default_factory=list)
    done: bool = False
    seconds: float = 0.0


@dataclass
class RunResult:
    ok: bool
    summary: str
    steps: list[StepOutcome] = field(default_factory=list)
    text: str = ""
    stopped_reason: str = "done"
    seconds: float = 0.0
    repairs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "text": self.text,
            "steps": len(self.steps),
            "tool_calls": sum(len(s.calls) for s in self.steps),
            "tool_errors": sum(1 for s in self.steps for r in s.results if not r.ok),
            "stopped_reason": self.stopped_reason,
            "seconds": round(self.seconds, 3),
            "repairs": self.repairs,
        }


class Agent:
    """Drives the model/tool loop until the task is done."""

    def __init__(
        self,
        provider: Any,
        *,
        registry: ToolRegistry | None = None,
        policy: Policy | None = None,
        session: Session | None = None,
        max_steps: int = 24,
        temperature: float = 0.4,
        max_tokens: int = 4000,
        context_max_tokens: int | None = 60_000,
        on_event: EventHandler | None = None,
        role: str = "coder",
        max_skills_context: int = 4,
        plugins: Any = None,
        healer: Any = None,
        mode: WorkMode | None = None,
        agent_id: str = "main",
        agent_name: str = "xli",
    ):
        self.provider = provider
        self.role = role
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.max_skills_context = max_skills_context
        self._skills: str | None = None
        # XPI plugins observe the loop through hooks. Injected rather than
        # looked up so tests (and an embedder) control exactly what is loaded.
        self.plugins = plugins
        # SelfHealingEngine: when set, a transient provider failure is retried
        # with backoff instead of ending the run on the first hiccup.
        self.healer = healer
        # Plan/build mode, when given, drives both the system message and the
        # permission posture, so the two cannot disagree. An explicit `policy`
        # still wins — the caller is closer to the truth about intent.
        self.mode = mode
        if policy is not None:
            self.policy = policy
        elif mode is not None:
            from xli.core.plan_build import policy_for

            self.policy = policy_for(mode)
        else:
            self.policy = Policy(mode=Mode.CONFIRM)
        self.registry = registry if registry is not None else default_registry(policy=self.policy)
        if self.registry.policy is None:
            self.registry.policy = self.policy
        self.session = session
        self.max_steps = max(1, int(max_steps))
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.context_max_tokens = context_max_tokens
        self.on_event = on_event or (lambda kind, payload: None)

    # ----------------------------------------------------------------- events
    def emit(self, kind: str, **payload: Any) -> None:
        """Fire a progress event; a throwing frontend must not kill the loop."""
        try:
            payload.setdefault("agent_id", self.agent_id)
            payload.setdefault("agent_name", self.agent_name)
            result = self.on_event(kind, payload)
            if asyncio.iscoroutine(result):
                asyncio.ensure_future(result)
        except Exception:  # noqa: BLE001 - rendering must never break the agent
            pass

    # ----------------------------------------------------------------- prompt
    def system_prompt(self, *, include_skills: bool = True) -> str:
        """Tool list plus the skills relevant to this agent.

        Skills are opt-out rather than opt-in: without them the corpus in
        `xli/skills/` is dead weight, and a few hundred bytes of relevant
        guidance reliably beats a model improvising from scratch.
        """
        prompt = SYSTEM_PROMPT_TEMPLATE.format(tools=self.registry.prompt_block())
        sub_agents = self._sub_agent_block()
        if sub_agents:
            prompt += f"\n{sub_agents}"
        if self.mode is not None:
            from xli.core.plan_build import MODE_CONFIGS

            prompt += f"\n{MODE_CONFIGS[self.mode].system_message}"
        if include_skills and self.skills_context:
            prompt += f"\n{self.skills_context}"
        return prompt

    def _sub_agent_block(self) -> str:
        """Who `delegate` can hand work to, spelled out.

        Without this the model invents plausible names ("researcher", "coder")
        that do not exist, `delegate` fails, and the whole feature looks
        broken. The list is read from the live registry, so a user-defined
        sub-agent shows up in the prompt the moment it is created.
        """
        if "delegate" not in self.known_tools:
            return ""
        try:
            from xli.agents.registry import get_registry

            specs = get_registry().summary()
        except Exception:  # noqa: BLE001 - delegation is optional
            return ""
        if not specs:
            return ""
        lines = [
            f"- {spec['name']}: {str(spec.get('description') or '').strip()[:110]}"
            for spec in specs
        ]
        return (
            "Sub-agents you can hand work to with `delegate` "
            "(use the exact name):\n" + "\n".join(lines)
        )

    @property
    def known_tools(self) -> set[str]:
        """Every registered tool name, so the parser can recognise untagged
        call JSON the model wrote as plain text and execute it instead of
        showing the user a blob of JSON as if it were an answer."""
        try:
            return set(self.registry.names(enabled_only=False))
        except Exception:  # noqa: BLE001 - parsing must work even without a registry
            return set()

    @property
    def skills_context(self) -> str:
        """Cached skills block, resolved once per agent."""
        if self._skills is None:
            try:
                from xli.core.skills import get_skills_manager

                self._skills = get_skills_manager().get_skills_context(
                    self.role, max_skills=self.max_skills_context
                )
            except Exception:  # noqa: BLE001 - skills are a bonus, never fatal
                self._skills = ""
        return self._skills

    def build_messages(self, task: str) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = [{"role": "system", "content": self.system_prompt()}]
        if self.session is not None:
            messages.extend(self.session.messages(max_tokens=self.context_max_tokens))
        else:
            messages.append({"role": "user", "content": task})
        if self.session is None or not any(m["role"] == "user" for m in messages[1:]):
            messages.append({"role": "user", "content": task})
        return messages

    # -------------------------------------------------------------------- run
    async def run(self, task: str) -> RunResult:
        from xli.tools.context import AgentContext, set_agent_context, reset_agent_context

        ctx = AgentContext(
            provider=self.provider,
            on_event=self.on_event,
            registry=self.registry,
            policy=self.policy,
            parent_agent_id=self.agent_id,
        )
        token = set_agent_context(ctx)
        try:
            return await self._run_inner(task)
        finally:
            reset_agent_context(token)

    async def _run_inner(self, task: str) -> RunResult:
        started = time.perf_counter()
        self.emit(EVENT_AGENT, phase="start", task=task, max_steps=self.max_steps)
        self.notify_plugins(
            "on_agent_start", task=task, max_steps=self.max_steps, mode=self.policy.mode.value
        )

        if self.session is not None:
            self.session.add_user(task)

        steps: list[StepOutcome] = []
        repairs: list[str] = []
        messages = self.build_messages(task)
        final_text = ""
        summary = ""
        stopped = "max_steps"

        parse_failures = 0
        for index in range(1, self.max_steps + 1):
            step_started = time.perf_counter()
            self.emit("step", index=index, max_steps=self.max_steps)

            try:
                raw = await self._chat_with_healing(messages)
            except Exception as exc:  # noqa: BLE001 - provider faults are reported, not fatal
                stopped = "provider_error"
                final_text = f"provider error: {type(exc).__name__}: {exc}"
                self.emit("error", message=final_text)
                break

            parsed: ParsedResponse = parse_response(raw or "", known_tools=self.known_tools)
            repairs.extend(parsed.repairs)

            if parsed.text:
                self.emit("assistant", text=parsed.text)
            for repair in parsed.repairs:
                self.emit("repair", detail=repair)

            messages.append({"role": "assistant", "content": raw or ""})

            step = StepOutcome(
                index=index,
                text=parsed.text,
                calls=parsed.calls,
                seconds=time.perf_counter() - step_started,
            )
            # The step event opens the step; this closes it with the duration,
            # which is the only number the run report and the TUI cannot
            # reconstruct from anything else.
            self.emit("step_done", index=index, seconds=round(step.seconds, 3))

            if parsed.calls:
                step.results = await self._run_calls(parsed.calls)
                for call, result in zip(parsed.calls, step.results):
                    self.emit(
                        "tool_result",
                        name=call.name,
                        args=call.args,
                        ok=result.ok,
                        summary=result.render(),
                        # Timing is what the run report graphs; without it the
                        # front ends could only say *what* ran, never what was
                        # slow.
                        duration_ms=round(result.duration_ms, 1),
                        error=result.error or "",
                    )
                messages.append(
                    {
                        "role": "user",
                        "content": self._format_results(parsed.calls, step.results, parsed.repairs),
                    }
                )

            if self.session is not None:
                self.session.add_assistant(
                    parsed.text, calls=[c.to_dict() for c in parsed.calls]
                )
                for call, result in zip(parsed.calls, step.results):
                    self.session.add_tool(call.name, result.render(), ok=result.ok)

            steps.append(step)

            if parsed.done:
                stopped = "done"
                summary = parsed.done_text or parsed.text
                final_text = parsed.text
                break

            if not parsed.calls:
                # A turn that *looks like* a failed tool call is not an answer.
                # The old behaviour stopped the run here and printed the failed
                # attempt to the user as if it were the agent's reply — which
                # is exactly what a broken call looks like from the outside:
                # prose that begins with a tool name and a file path. Ask for
                # it again, in the right shape, before giving up on the turn.
                if parse_failures < MAX_PARSE_RECOVERIES and self._looks_like_call(raw, parsed):
                    parse_failures += 1
                    stopped = "parse_retry"
                    detail = self._recovery_hint(raw, parsed)
                    self.emit("repair", detail=detail)
                    messages.append({"role": "user", "content": detail})
                    continue

                if parse_failures >= MAX_PARSE_RECOVERIES:
                    # It was asked, more than once, to write a callable call and
                    # did not. Say that in the log instead of pretending the
                    # half-call was an answer; the text still reaches the user,
                    # because it is all the model actually said.
                    self.emit(
                        "repair",
                        detail=(
                            f"the model produced an unparseable tool call "
                            f"{parse_failures} times in a row; showing its text as-is"
                        ),
                    )

                # The model talked but asked for nothing, and did not say done.
                # Treat that as completion rather than looping until the budget
                # burns out — it has nothing left to do.
                stopped = "no_tool_calls"
                summary = parsed.text
                final_text = parsed.text
                break
        else:
            self.emit("warning", message=f"hit the {self.max_steps}-step limit")

        seconds = time.perf_counter() - started
        self.notify_plugins(
            "on_agent_end",
            ok=stopped in ("done", "no_tool_calls"),
            summary=summary,
            steps=len(steps),
            stopped_reason=stopped,
        )
        self.emit(
            EVENT_AGENT,
            phase="end",
            stopped_reason=stopped,
            steps=len(steps),
            seconds=round(seconds, 3),
        )

        return RunResult(
            ok=stopped in ("done", "no_tool_calls"),
            summary=summary.strip() or final_text.strip(),
            steps=steps,
            text=final_text,
            stopped_reason=stopped,
            seconds=seconds,
            repairs=repairs,
        )

    # ----------------------------------------------------------------- healing
    async def _chat_with_healing(self, messages: list) -> str:
        """Call the provider, retrying transient failures if a healer is set.

        Only the *classification* decides whether to retry: a 429 or a 503 gets
        another attempt with backoff, while an auth failure or a syntax error
        fails immediately, because retrying those just burns time and quota.
        """
        try:
            return await self.provider.chat(
                messages, temperature=self.temperature, max_tokens=self.max_tokens
            )
        except Exception as exc:
            if self.healer is None:
                raise

            analysis = await self.healer.analyze_error(exc, context="provider.chat")
            if not analysis.retryable:
                raise

            self.emit(
                "repair",
                detail=f"retrying {analysis.error_type}: {analysis.suggested_approach}",
            )
            return await self.healer.retry_with_backoff(
                self.provider.chat,
                messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )

    # ------------------------------------------------------- call recovery
    def _looks_like_call(self, raw: str, parsed: ParsedResponse) -> bool:
        """Did this turn try to call a tool and fail to be understood?

        Three signals, cheapest first: the parser said it skipped something,
        the raw text carries a tool tag, or it names a registered tool followed
        by parentheses — the `read(path="x.py")` shape weaker models fall back
        to when a tag is dropped.
        """
        if any(
            "unparseable" in repair or "no 'name'" in repair for repair in parsed.repairs
        ):
            return True
        if "<tool" in raw or "tool_calls" in raw or "function_call" in raw:
            return True
        # A bare JSON object shaped like a call — with a name and an argument
        # object — but naming something unregistered is still an attempt. The
        # parser correctly refuses to execute it; the agent's job is to say so
        # and give the model the list of names it may use.
        from xli.parse import _CALL_KEYS, json_spans, loads_lenient

        for start, end in json_spans(raw):
            try:
                value, _repairs = loads_lenient(raw[start:end])
            except Exception:  # noqa: BLE001 - unparsable prose is prose
                continue
            if not isinstance(value, dict):
                continue
            has_args = any(isinstance(value.get(key), dict) for key in ("args", "arguments", "params"))
            if has_args and any(key in value for key in ("name", "tool", "action")):
                return True
            if value and set(value) <= _CALL_KEYS and any(
                key in value for key in ("name", "tool", "action")
            ):
                return True

        known = self.known_tools
        if not known:
            return False
        # `read(path="x.py")`, not a prose mention of `read(x)`: the call
        # signal is a name followed by parentheses that contain something
        # value-shaped.
        for match in re.finditer(r"\b([a-z_][a-z0-9_]{2,40})\s*\([^)]*[=\"'{]", raw):
            name = match.group(1)
            if name in known or self.registry.resolve(name) in known:
                return True
        return False

    def _recovery_hint(self, raw: str, parsed: ParsedResponse) -> str:
        """What to tell the model so its next attempt is callable."""
        from xli.parse import render_call

        problems = "; ".join(dict.fromkeys(parsed.repairs)) or "no call was recognised"
        example = render_call(
            ToolCall(
                name="read",
                args={"path": "xli/parse/__init__.py"},
            )
        )
        return (
            "Your previous reply was not a tool call this agent could run "
            f"({problems}). If you meant to call a tool, reply with exactly one "
            f"call per tag, like this: {example} — and nothing else inside the "
            "tag. If you meant to answer the user, reply with plain prose and no "
            "tool syntax. Do not repeat the previous form."
        )

    # ---------------------------------------------------------------- plugins
    def notify_plugins(self, hook: str, **context: Any) -> None:
        """Broadcast an XPI hook. Plugins must never be able to stop the agent."""
        if self.plugins is None:
            return
        try:
            report = self.plugins.dispatch(hook, **context)
        except Exception:  # noqa: BLE001 - plugin system failure is not fatal
            return
        for failure in report.errors:
            self.emit(
                "warning",
                message=f"plugin {failure['plugin']} failed in {hook}: {failure['error']}",
            )

    # ------------------------------------------------------------------ tools
    async def _run_calls(self, calls: list[ToolCall]) -> list[ToolResult]:
        """Run the turn's calls, concurrently when that is safe.

        Order matters when calls share state: two writes to the same file, or a
        write followed by a test that reads it, must not race. Reads have no
        such order, and neither do sub-agents — so a turn that only reads, or
        that delegates to three reviewers, runs them together. That is the
        difference between delegating to four sub-agents in four seconds and in
        sixteen, and it is why `delegate` is worth having at all.
        """
        if len(calls) > 1 and self._can_run_concurrently(calls):
            for call in calls:
                self.emit("tool_call", name=call.name, args=call.args)
                self.notify_plugins("on_tool_call", name=call.name, args=call.args)
            results = list(
                await asyncio.gather(
                    *(self.registry.execute(call.name, call.args) for call in calls)
                )
            )
            for call, result in zip(calls, results):
                self.notify_plugins(
                    "on_tool_result", name=call.name, ok=result.ok, summary=result.render()
                )
            return results

        results: list[ToolResult] = []
        for call in calls:
            self.emit("tool_call", name=call.name, args=call.args)
            self.notify_plugins("on_tool_call", name=call.name, args=call.args)
            result = await self.registry.execute(call.name, call.args)
            self.notify_plugins(
                "on_tool_result", name=call.name, ok=result.ok, summary=result.render()
            )
            results.append(result)
        return results

    def _can_run_concurrently(self, calls: list[ToolCall]) -> bool:
        """True when no call can observe another call's side effects."""
        try:
            from xli.tools.base import is_mutating

            return not any(is_mutating(call.name, self.registry) for call in calls)
        except Exception:  # noqa: BLE001 - when unsure, be sequential
            return False

    @staticmethod
    def _format_results(
        calls: list[ToolCall], results: list[ToolResult], repairs: list[str]
    ) -> str:
        """Render results back into the conversation for the next turn."""
        lines: list[str] = []
        for call, result in zip(calls, results):
            status = "OK" if result.ok else "ERROR"
            body = result.data if result.ok else result.error
            if not isinstance(body, str):
                body = json.dumps(body, ensure_ascii=False, default=str)
            if len(body) > 8000:
                body = body[:4000] + f"\n... [{len(body) - 8000} chars truncated] ...\n" + body[-4000:]
            lines.append(f"[{status}] {call.name}: {body}")

        if repairs:
            lines.append(
                "Note: your previous reply needed repairing to be parsed: "
                + "; ".join(dict.fromkeys(repairs))
                + ". Emit valid JSON inside the tool tags."
            )
        return "\n".join(lines)


def run_sync(
    task: str,
    provider: Any,
    *,
    on_event: EventHandler | None = None,
    **kwargs: Any,
) -> RunResult:
    """Blocking convenience wrapper for scripts and the Neovim bridge."""
    return asyncio.run(Agent(provider, on_event=on_event, **kwargs).run(task))
