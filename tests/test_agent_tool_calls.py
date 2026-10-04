#!/usr/bin/env python3
"""The agent side of tool calls: recovery, aliases, and real sub-agents.

Three bugs are pinned here, all of them visible to the user as "the agent does
not do anything":

  * a reply that *tried* to call a tool and failed to be understood used to end
    the run, with the failed attempt printed as the answer.
  * `delegate` was defined and documented but never registered, so every
    sub-agent call was an "unknown tool" error.
  * a typo'd or third-party tool name (`read_file`, `run_command`) was refused
    instead of resolved.
"""

import asyncio
import json

from xli.agent import Agent
from xli.permissions.policy import Mode, Policy
from xli.providers.fake import FakeProvider
from xli.tools.registry import default_registry


def run(responses, task="сделай", **kwargs):
    """Drive one agent turn synchronously and return (result, events)."""
    events = []

    def on_event(kind, payload):
        events.append((kind, dict(payload)))

    agent = Agent(
        FakeProvider(responses=responses),
        registry=default_registry(policy=Policy(mode=Mode.AUTO)),
        on_event=on_event,
        max_steps=kwargs.pop("max_steps", 8),
        **kwargs,
    )
    return asyncio.run(agent.run(task)), events


def calls_of(result):
    return [call.name for step in result.steps for call in step.calls]


class TestTheCallIsExecutedNotQuoted:
    def test_a_broken_call_is_re_asked_and_then_works(self):
        """The model writes prose, gets told, and replies in the right shape."""
        result, events = run(
            [
                'Сейчас прочитаю файл read(path="xli/parse/__init__.py")',  # inline form
                '<tool>{"name": "ls", "args": {"path": "xli"}}</tool>',
                "<done>готово</done>",
            ]
        )
        assert result.stopped_reason == "done"
        assert "ls" in calls_of(result)
        assert any(kind == "repair" for kind, _ in events), "the model was told what was wrong"

    def test_an_answer_that_merely_mentions_a_tool_is_still_an_answer(self):
        result, _ = run(["Привет. Инструмент read я не вызываю, просто упоминаю."])
        assert result.stopped_reason == "no_tool_calls"
        assert "упоминаю" in result.summary

    def test_a_call_written_as_json_text_executes(self):
        raw = json.dumps({"name": "ls", "args": {"path": "xli"}})
        result, _ = run([raw, "<done>ок</done>"])
        assert "ls" in calls_of(result)


class TestUnknownNamesAreResolved:
    def test_a_foreign_tool_name_is_aliased_once_the_model_is_told(self):
        """The first reply names a tool this agent does not have.

        It must not be run (prose can name anything) and it must not end the
        run either: the model is told the shape was not understood, and its
        second reply — with the agent's own name — runs.
        """
        first = json.dumps({"name": "read_file", "args": {"path": "README.md"}})
        second = '<tool>{"name": "ls", "args": {"path": "xli"}}</tool>'
        result, events = run([first, second, "<done>ок</done>"])
        assert any(kind == "repair" for kind, _ in events)
        assert "ls" in calls_of(result)

    def test_an_unknown_name_gets_the_list_of_real_ones(self):
        result, events = run(
            [
                json.dumps({"name": "frobnicate", "args": {}}),
                json.dumps({"name": "read", "args": {"path": "README.md"}}),
                "<done>ок</done>",
            ]
        )
        repairs = [payload["detail"] for kind, payload in events if kind == "repair"]
        assert repairs and "read" in repairs[0], "the correction names a callable shape"

    def test_prose_json_is_still_prose(self):
        """`{"name": "demo", "port": 8080}` is not a call and must not loop."""
        result, events = run(['Пример конфига: {"name": "demo", "port": 8080, "debug": true}'])
        assert result.stopped_reason == "no_tool_calls"
        assert not [kind for kind, _ in events if kind == "repair"]


class TestGiveUpIsVisible:
    def test_a_model_that_never_learns_does_not_loop_forever(self):
        """Three chances, then the text is shown with an explanation."""
        result, events = run(['read(path=config_file)'] * 5, max_steps=8)
        assert result.stopped_reason == "no_tool_calls"
        repairs = [payload["detail"] for kind, payload in events if kind == "repair"]
        hints = [detail for detail in repairs if "not a tool call" in detail]
        assert len(hints) == 3, "exactly MAX_PARSE_RECOVERIES attempts"
        assert any("as-is" in detail for detail in repairs), "giving up is said out loud"
        assert "config_file" in result.summary

    def test_a_prose_mention_of_a_tool_name_is_not_a_failed_call(self):
        result, events = run(["Функция read(x) читает файл, но я ничего не вызываю."])
        assert result.stopped_reason == "no_tool_calls"
        assert not [kind for kind, _ in events if kind == "repair"]


class TestSubAgentsAreReal:
    def test_delegate_is_registered_at_all(self):
        registry = default_registry()
        assert "delegate" in registry.names()

    def test_the_system_prompt_names_the_available_agents(self):
        agent = Agent(FakeProvider(responses=[]), registry=default_registry())
        prompt = agent.system_prompt(include_skills=False)
        assert "delegate" in prompt
        assert "explorer" in prompt, "inventing agent names must not be necessary"

    def test_a_delegate_call_runs_a_sub_agent(self):
        raw = json.dumps(
            {
                "name": "delegate",
                "args": {"agent_name": "explorer", "task": "найди точку входа"},
            }
        )
        result, events = run([raw, "<done>ок</done>"])
        names = [payload.get("agent_name") for kind, payload in events if kind == "agent"]
        assert "explorer" in names, "the sub-agent actually started"
        assert "delegate" in calls_of(result)

    def test_several_delegates_in_one_turn_run_together(self):
        raw = "".join(
            json.dumps({"name": "delegate", "args": {"agent_name": name, "task": f"проверь {name}"}})
            for name in ("reviewer", "explorer", "debugger")
        )
        result, events = run([raw, "<done>ок</done>"])
        started = [
            payload.get("agent_name")
            for kind, payload in events
            if kind == "agent" and payload.get("phase") == "start"
        ]
        assert {"reviewer", "explorer", "debugger"} <= set(started)
        assert calls_of(result).count("delegate") == 3

    def test_an_unknown_agent_name_is_an_error_not_a_crash(self):
        raw = json.dumps({"name": "delegate", "args": {"agent_name": "nobody", "task": "x"}})
        _result, events = run([raw, "<done>ок</done>"])
        failures = [
            payload for kind, payload in events if kind == "tool_result" and not payload.get("ok")
        ]
        assert failures, "the model is told the agent name does not exist"


class TestParallelismIsSafe:
    def test_reads_run_concurrently_and_writes_do_not(self):
        from xli.tools.base import is_mutating

        registry = default_registry()
        assert not is_mutating("read", registry)
        assert not is_mutating("delegate", registry)
        assert is_mutating("write", registry)
        assert is_mutating("bash", registry)
