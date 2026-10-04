#!/usr/bin/env python3
"""Sub-agents, end to end — the child's answer has to reach the parent.

`delegate` used to return bookkeeping only: agent id, step count, seconds.
The agent loop feeds ``result.data`` back to the model, and ``result.render()``
is what the transcript shows a human, so the parent read

    [OK] delegate: {"agent_id": "reviewer-1a2b", "ok": true, "steps": 1}

and learned *nothing* the sub-agent had said. The only sensible reaction to
that is to delegate the same task again, and again, until the step budget runs
out — which is precisely what "суб-агенты не работают" looks like from the
outside: a transcript full of ❯ delegate with no report anywhere.

These tests drive real Agent objects with a scripted provider, so they check
the whole path: parent prompt → delegate → child agent → child's tools →
child's answer → parent's next prompt → done.
"""

import asyncio
import json

import pytest

from xli.agent import Agent
from xli.permissions.policy import Mode, Policy
from xli.providers.fake import FakeProvider
from xli.tools.registry import default_registry

PARENT_CAN_DELEGATE = "delegate" in " ".join(  # children get a filtered registry
    default_registry(policy=Policy(mode=Mode.AUTO)).names()
)


def call(name: str, **args) -> str:
    return "<tool>" + json.dumps({"name": name, "args": args}, ensure_ascii=False) + "</tool>"


class ScriptedProvider(FakeProvider):
    """One provider, two actors: parent and sub-agent get different scripts.

    A sub-agent is built with the tools its spec allows, and none of the
    built-in specs allow `delegate`, so the word appears in the parent's system
    prompt and nowhere in the child's.
    """

    def __init__(self, parent, child):
        super().__init__(responses=[""])
        self.parent_script = list(parent)
        self.child_script = list(child)

    def _next_response(self, messages):
        self.calls.append(messages)
        system = str(messages[0].get("content", ""))
        script = self.parent_script if "delegate" in system else self.child_script
        if not script:
            return "<done>конец</done>"
        return script.pop(0) if len(script) > 1 else script[0]


def drive(provider, task="проверь парсер", **kwargs):
    events = []

    def on_event(kind, payload):
        events.append((kind, dict(payload)))

    agent = Agent(
        provider,
        registry=default_registry(policy=Policy(mode=Mode.AUTO)),
        policy=Policy(mode=Mode.AUTO),
        on_event=on_event,
        max_steps=kwargs.pop("max_steps", 8),
        **kwargs,
    )
    return asyncio.run(agent.run(task)), events


def delegate_args(result):
    return [
        call.args
        for step in result.steps
        for call in step.calls
        if call.name == "delegate"
    ]


class TestTheChildAnswerComesBack:
    def test_the_parent_reads_what_the_child_answered(self):
        provider = ScriptedProvider(
            parent=[call("delegate", agent_name="explorer", task="найди точку входа"), "<done>готово</done>"],
            child=["Точка входа — xli/cli.py:main.\n<done>нашёл</done>"],
        )
        result, _ = drive(provider)

        # every prompt the parent saw after delegating must carry the report
        parent_turns = [
            "\n".join(str(m.get("content", "")) for m in messages)
            for messages in provider.calls[1:]
            if "delegate" in str(messages[0].get("content", ""))
        ]
        assert parent_turns, "the parent was asked again after the delegation"
        assert any("xli/cli.py:main" in turn for turn in parent_turns), (
            "the sub-agent's answer never reached the parent"
        )

    def test_a_finished_delegate_is_not_re_delegated_forever(self):
        """The regression: no report in, same call out, until max_steps."""
        provider = ScriptedProvider(
            parent=[call("delegate", agent_name="reviewer", task="посмотри парсер"), "<done>готово</done>"],
            child=["Замечаний нет.\n<done>чисто</done>"],
        )
        result, _ = drive(provider, max_steps=6)

        assert len(delegate_args(result)) == 1, "the parent delegated once, not six times"
        assert result.stopped_reason == "done"
        assert result.ok

    def test_the_data_carries_the_text(self):
        """`data` is the model-facing field, `summary` is the human-facing one."""
        from xli.tools.builtin import delegate

        provider = ScriptedProvider(parent=["<done>x</done>"], child=["Ответ ребёнка.\n<done>ок</done>"])
        from xli.tools.context import AgentContext, reset_agent_context, set_agent_context

        registry = default_registry(policy=Policy(mode=Mode.AUTO))
        ctx = AgentContext(
            provider=provider, registry=registry, policy=Policy(mode=Mode.AUTO)
        )
        token = set_agent_context(ctx)
        try:
            result = asyncio.run(
                registry.execute("delegate", {"agent_name": "reviewer", "task": "посмотри"})
            )
        finally:
            reset_agent_context(token)

        assert result.ok, result.error
        assert "Ответ ребёнка" in result.data["text"]
        assert result.data["agent_name"] == "reviewer"
        assert result.data["steps"] >= 1
        # and the transcript line still fits on one row, in Russian
        assert result.render().startswith("[reviewer:")


class TestTheSubAgentIsARealSeparateActor:
    def test_its_events_are_tagged_with_its_own_identity(self):
        provider = ScriptedProvider(
            parent=[call("delegate", agent_name="explorer", task="найди точку входа"), "<done>ок</done>"],
            child=["Прочитал.\n<done>ок</done>"],
        )
        _, events = drive(provider)

        ids = {payload.get("agent_id") for _, payload in events}
        assert "main" in ids
        assert len(ids) >= 2, "the child ran under its own id, not the parent's"
        names = {payload.get("agent_name") for _, payload in events}
        assert "explorer" in names and "xli" in names

    def test_the_child_runs_tools_from_its_own_registry(self, tmp_path):
        """A readonly sub-agent cannot write, however its task is phrased."""
        target = tmp_path / "hack.txt"
        provider = ScriptedProvider(
            parent=[
                call("delegate", agent_name="reviewer", task=f"запиши файл {target}"),
                "<done>ок</done>",
            ],
            child=[call("write", path=str(target), content="нет"), "Не могу.\n<done>ок</done>"],
        )
        result, events = drive(provider, max_steps=4)

        assert not target.exists(), "a readonly sub-agent wrote a file"
        failures = [
            payload
            for kind, payload in events
            if kind == "tool_result" and not payload.get("ok")
        ]
        assert failures, "the child was told the tool is not available"
        assert all(call.name != "write" for step in result.steps for call in step.calls)

    def test_an_unknown_agent_names_the_real_ones(self):
        provider = ScriptedProvider(
            parent=[call("delegate", agent_name="никто", task="x"), "<done>ок</done>"],
            child=["<done>ок</done>"],
        )
        _, events = drive(provider)
        errors = [
            str(payload.get("error", "")) + str(payload.get("summary", ""))
            for kind, payload in events
            if kind == "tool_result" and not payload.get("ok")
        ]
        assert errors and any("reviewer" in text for text in errors), errors

    def test_nesting_stops_before_it_becomes_a_fractal(self):
        """A delegate at the bottom of the chain is refused, not run.

        No built-in spec is allowed to delegate, so the guard is reached
        directly: a context already at MAX_DELEGATE_DEPTH must be told no.
        """
        from xli.tools.builtin import MAX_DELEGATE_DEPTH
        from xli.tools.context import AgentContext, reset_agent_context, set_agent_context

        assert MAX_DELEGATE_DEPTH >= 1
        registry = default_registry(policy=Policy(mode=Mode.AUTO))
        ctx = AgentContext(
            provider=FakeProvider(responses=["<done>x</done>"]),
            registry=registry,
            policy=Policy(mode=Mode.AUTO),
            depth=MAX_DELEGATE_DEPTH,
        )
        token = set_agent_context(ctx)
        try:
            result = asyncio.run(
                registry.execute("delegate", {"agent_name": "explorer", "task": "и ещё раз"})
            )
        finally:
            reset_agent_context(token)

        assert not result.ok
        assert "depth" in (result.error or ""), result.error


class TestDelegationWithoutAContextIsSaidOutLoud:
    def test_the_tool_explains_itself_outside_an_agent(self):
        registry = default_registry(policy=Policy(mode=Mode.AUTO))
        result = asyncio.run(
            registry.execute("delegate", {"agent_name": "explorer", "task": "x"})
        )
        assert not result.ok
        assert "agent context" in (result.error or "")


class TestDelegatesAreDifferentColours:
    def test_two_delegates_never_share_a_colour(self):
        from xli.ui.agents import agent_color

        names = ["reviewer", "explorer", "debugger", "test-writer", "documenter"]
        colours = [agent_color(name) for name in names]
        assert len(set(colours)) == len(names), colours

    def test_the_colour_survives_a_restart(self):
        from xli.ui.agents import agent_color

        assert agent_color("reviewer") == agent_color("reviewer")
        assert agent_color("reviewer") != agent_color("explorer")

    def test_the_spec_colour_wins_when_it_names_one(self):
        from xli.ui.agents import agent_color

        assert agent_color("reviewer", "good") != agent_color("reviewer", "accent")


@pytest.mark.parametrize(
    "seconds,expected",
    [(0.0, "0,0 с"), (3.44, "3,4 с"), (59.9, "59,9 с"), (125.0, "2 мин 05 с")],
)
def test_seconds_are_written_the_russian_way(seconds, expected):
    from xli.tools.builtin import _ru_seconds

    assert _ru_seconds(seconds) == expected


def test_delegate_is_advertised_in_the_parent_prompt():
    agent = Agent(FakeProvider(responses=[""]), registry=default_registry(policy=Policy(mode=Mode.AUTO)))
    prompt = agent.system_prompt(include_skills=False)
    assert "delegate" in prompt
    for name in ("reviewer", "explorer", "debugger"):
        assert name in prompt
