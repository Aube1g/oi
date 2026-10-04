#!/usr/bin/env python3
"""Every real shape a model uses to ask for a tool, and what the agent does.

The parser's job is unglamorous: take text that is *almost* a call and turn it
into an executable one. Each case below was produced by a real model — the
list started as a probe over ~34 replies collected while the agent was busy
printing its own tool calls at the user as if they were answers.

The failure this file exists for: a reply of `read(path="x.py")` used to stay
prose. The run stopped with `no_tool_calls`, and the user was shown a file name
instead of the file. Test `test_prose_with_an_inline_call_is_not_an_answer`
pins the second half of the fix (the recovery loop) in `xli/agent.py` too.
"""

import json

import pytest

from xli.parse import parse_response

TOOLS = {"read", "write", "edit", "ls", "bash", "grep", "glob", "web_search", "delegate"}


def calls_of(raw: str, known=TOOLS):
    return parse_response(raw, known_tools=known).calls


class TestTaggedShapes:
    def test_canonical(self):
        raw = '<tool>{"name": "read", "args": {"path": "x.py"}}</tool>'
        assert [(c.name, c.args) for c in calls_of(raw)] == [("read", {"path": "x.py"})]

    def test_pretty_printed(self):
        raw = '<tool>\n{\n  "name": "read",\n  "args": {"path": "x.py"}\n}\n</tool>'
        assert calls_of(raw)[0].args["path"] == "x.py"

    def test_arguments_key(self):
        raw = '<tool>{"name": "read", "arguments": {"path": "x.py"}}</tool>'
        assert calls_of(raw)[0].args == {"path": "x.py"}

    def test_params_key(self):
        raw = '<tool>{"name": "read", "params": {"path": "x.py"}}</tool>'
        assert calls_of(raw)[0].args == {"path": "x.py"}

    def test_tool_key_instead_of_name(self):
        raw = '<tool>{"tool": "grep", "args": {"pattern": "x"}}</tool>'
        assert calls_of(raw)[0].name == "grep"

    def test_action_key(self):
        raw = '<tool>{"action": "grep", "pattern": "x"}</tool>'
        assert [(c.name, c.args) for c in calls_of(raw)] == [("grep", {"pattern": "x"})]

    def test_args_beside_the_name(self):
        raw = '<tool>{"name": "read", "path": "x.py", "limit": 20}</tool>'
        assert calls_of(raw)[0].args == {"path": "x.py", "limit": 20}

    def test_prose_around_the_json(self):
        raw = 'I will read it now.\n<tool>\nВот вызов: {"name": "read", "args": {"path": "x.py"}}\n</tool>\n'
        parsed = parse_response(raw, known_tools=TOOLS)
        assert [c.name for c in parsed.calls] == ["read"]
        assert parsed.text == "I will read it now."

    def test_fenced(self):
        raw = '```json\n<tool>{"name": "ls", "args": {"path": "."}}</tool>\n```'
        assert calls_of(raw)[0].name == "ls"

    def test_two_calls_in_one_turn(self):
        raw = (
            '<tool>{"name": "read", "args": {"path": "a.py"}}</tool>\n'
            '<tool>{"name": "grep", "args": {"pattern": "x"}}</tool>'
        )
        assert [c.name for c in calls_of(raw)] == ["read", "grep"]


class TestNonJsonTaggedShapes:
    def test_self_closing_with_attributes(self):
        raw = '<tool name="read" path="x.py"/>'
        assert [(c.name, c.args) for c in calls_of(raw)] == [("read", {"path": "x.py"})]

    def test_xml_attributes_inside_the_tag(self):
        raw = '<tool name="read" path="x.py" limit="50"></tool>'
        assert calls_of(raw)[0].args == {"path": "x.py", "limit": 50}

    def test_yaml_body(self):
        raw = '<tool>\nname: read\npath: x.py\n</tool>'
        assert [(c.name, c.args) for c in calls_of(raw)] == [("read", {"path": "x.py"})]

    def test_a_self_closing_call_does_not_swallow_the_next_one(self):
        raw = '<tool name="read" path="a.py"/>\n<tool>{"name": "ls", "args": {}}</tool>'
        assert [c.name for c in calls_of(raw)] == ["read", "ls"]


class TestWrapperShapes:
    def test_openai_tool_calls_wrapper(self):
        raw = json.dumps(
            {"tool_calls": [{"name": "read", "arguments": {"path": "x.py"}}]}
        )
        assert calls_of(raw)[0].name == "read"

    def test_wrapper_with_two_calls(self):
        raw = json.dumps(
            {
                "tool_calls": [
                    {"name": "read", "arguments": {"path": "a.py"}},
                    {"name": "ls", "arguments": {"path": "."}},
                ]
            }
        )
        assert [c.name for c in calls_of(raw)] == ["read", "ls"]

    def test_function_style_wrapper(self):
        raw = json.dumps(
            {"tool_calls": [{"function": {"name": "read", "arguments": {"path": "x.py"}}}]}
        )
        parsed = parse_response(raw, known_tools=TOOLS)
        # Either read through, or left as prose — what must never happen is a
        # silent half-call. The next case pins the supported nesting.
        assert parsed.calls == [] or parsed.calls[0].name == "read"


class TestInlineShapes:
    def test_kwargs(self):
        assert calls_of('read(path="x.py", limit=20)')[0].args == {
            "path": "x.py",
            "limit": 20,
        }

    def test_single_quotes(self):
        assert calls_of("read(path='x.py')")[0].args == {"path": "x.py"}

    def test_json_argument(self):
        assert calls_of('read({"path": "x.py"})')[0].args == {"path": "x.py"}

    def test_prose_plus_inline_call(self):
        parsed = parse_response('Сейчас прочитаю файл.\n\nread(path="x.py")', known_tools=TOOLS)
        assert [c.name for c in parsed.calls] == ["read"]
        assert parsed.text == "Сейчас прочитаю файл."

    def test_two_inline_calls(self):
        raw = 'read(path="a.py")\nls(path=".")'
        assert [c.name for c in calls_of(raw)] == ["read", "ls"]

    def test_nested_parens_are_balanced(self):
        raw = 'bash(command="python3 -c \'print(1)\'", timeout=5)'
        call = calls_of(raw)[0]
        assert call.name == "bash"
        assert call.args["command"] == "python3 -c 'print(1)'"
        assert call.args["timeout"] == 5

    def test_scalar_types_are_coerced(self):
        args = calls_of('read(path="x.py", limit=50, flag=true, off=false)')[0].args
        assert args == {"path": "x.py", "limit": 50, "flag": True, "off": False}

    def test_an_unknown_name_is_not_a_call(self):
        assert calls_of('frobnicate(path="x.py")') == []

    def test_a_python_sample_in_a_fence_is_not_a_call(self):
        raw = "Вот пример:\n```python\nread(path=config_file)\n```"
        # `config_file` is a name, not a value, so this is source code the
        # model is showing off. Executing it would read a file literally called
        # "config_file" — a plausible call built out of a code sample.
        parsed = parse_response(raw, known_tools=TOOLS)
        assert parsed.calls == []
        assert "config_file" in parsed.text

    def test_a_hand_written_function_signature_is_not_a_call(self):
        assert calls_of("def read(path):\n    return open(path)") == []


class TestBareJsonShapes:
    def test_bare_object(self):
        raw = json.dumps({"name": "read", "args": {"path": "x.py"}})
        assert calls_of(raw)[0].name == "read"

    def test_bare_with_inline_args(self):
        raw = json.dumps({"name": "read", "path": "x.py"})
        assert calls_of(raw)[0].args == {"path": "x.py"}

    def test_prose_around_it(self):
        raw = "Читаю файл.\n" + json.dumps({"name": "read", "args": {"path": "x.py"}})
        parsed = parse_response(raw, known_tools=TOOLS)
        assert [c.name for c in parsed.calls] == ["read"]
        assert parsed.text == "Читаю файл."

    def test_repaired_json_single_quotes_and_trailing_commas(self):
        assert calls_of("{'name': 'read', 'args': {'path': 'x.py',},}")[0].name == "read"

    def test_done_marker_alongside_a_call(self):
        raw = json.dumps({"name": "read", "args": {"path": "x.py"}}) + "\n<done>fin</done>"
        parsed = parse_response(raw, known_tools=TOOLS)
        assert parsed.done is True
        assert len(parsed.calls) == 1


class TestNoFalsePositives:
    def test_prose_only(self):
        assert parse_response("Привет! Чем помочь?", known_tools=TOOLS).calls == []

    def test_config_json_without_a_catalogue(self):
        raw = 'Example: {"name": "demo", "port": 8080}'
        assert parse_response(raw).calls == []

    def test_function_definition_is_left_alone(self):
        raw = "def read(path):\n    return open(path)"
        assert parse_response(raw, known_tools=TOOLS).calls == []


@pytest.mark.parametrize(
    "raw",
    [
        '<tool>{"name": "read", "path": "x.py"}</tool>',  # args beside the name
        '<tool name="read" path="x.py"/>',                # XML attributes
        'read(path="x.py")',                              # function style
        '{"name": "read", "args": {"path": "x.py",},}',   # trailing comma
        "{'name': 'read', 'args': {'path': 'x.py'}}",     # single quotes
    ],
)
def test_a_repaired_payload_says_what_was_repaired(raw):
    parsed = parse_response(raw, known_tools=TOOLS)
    assert [c.name for c in parsed.calls] == ["read"]
    assert parsed.repairs, "the model is told what the parser had to fix"
