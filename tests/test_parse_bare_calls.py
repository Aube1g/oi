"""The model sometimes writes its tool calls as bare JSON, no <tool> wrapper.

That used to reach the user verbatim as if it were an answer, with the loop
stopping at `no_tool_calls`. The parser now extracts such calls and executes
them; these tests pin that behaviour and its false-positive guards.
"""

from __future__ import annotations

import json

from xli.parse import StreamParser, parse_response

TOOLS = {"read", "grep", "write", "bash"}


class TestBareJsonCallsAreExecuted:
    def test_a_bare_call_is_extracted_and_the_prose_is_clean(self):
        raw = (
            "Сори, я читал код. Сейчас найду палитру.\n"
            + json.dumps({"name": "grep", "args": {"pattern": "ACCENT", "path": "xli/tui/app.py"}})
        )
        parsed = parse_response(raw, known_tools=TOOLS)
        assert [c.name for c in parsed.calls] == ["grep"]
        assert parsed.calls[0].args["pattern"] == "ACCENT"
        assert "grep" not in parsed.text
        assert "палитру" in parsed.text

    def test_without_a_catalogue_only_the_call_shape_is_accepted(self):
        raw = json.dumps({"name": "anything", "args": {}})
        parsed = parse_response(raw)
        assert [c.name for c in parsed.calls] == ["anything"]

    def test_prose_json_with_extra_keys_is_left_alone_without_a_catalogue(self):
        raw = 'Example config: {"name": "demo", "port": 8080, "debug": true}'
        parsed = parse_response(raw)
        assert parsed.calls == []
        assert "port" in parsed.text

    def test_an_unknown_tool_name_is_not_executed_when_catalogue_given(self):
        raw = json.dumps({"name": "frobnicate", "args": {}})
        parsed = parse_response(raw, known_tools=TOOLS)
        assert parsed.calls == []
        assert "frobnicate" in parsed.text

    def test_tagged_calls_are_not_doubled_by_the_bare_scan(self):
        tagged = '<tool>{"name": "read", "args": {"path": "a.py"}}</tool>'
        parsed = parse_response(tagged, known_tools=TOOLS)
        assert [c.name for c in parsed.calls] == ["read"]

    def test_mixed_tagged_and_bare_both_run(self):
        raw = (
            '<tool>{"name": "read", "args": {"path": "a.py"}}</tool>\n'
            + json.dumps({"name": "grep", "args": {"pattern": "x"}})
        )
        parsed = parse_response(raw, known_tools=TOOLS)
        assert [c.name for c in parsed.calls] == ["read", "grep"]

    def test_the_repair_is_reported_so_the_model_hears_about_it(self):
        raw = json.dumps({"name": "read", "args": {"path": "a.py"}})
        parsed = parse_response(raw, known_tools=TOOLS)
        assert any("untagged" in r for r in parsed.repairs)

    def test_done_marker_still_wins_over_a_bare_call(self):
        raw = json.dumps({"name": "read", "args": {"path": "a.py"}}) + "\n<done>fin</done>"
        parsed = parse_response(raw, known_tools=TOOLS)
        assert parsed.done is True
        assert parsed.done_text == "fin"
        assert len(parsed.calls) == 1

    def test_nested_args_survive_the_span_scan(self):
        raw = json.dumps(
            {"name": "bash", "args": {"command": "echo {a: 1}", "env": {"X": "1"}}}
        )
        parsed = parse_response(raw, known_tools=TOOLS)
        assert len(parsed.calls) == 1
        assert parsed.calls[0].args["env"] == {"X": "1"}

    def test_lenient_payloads_single_quotes_still_extract(self):
        raw = "{'name': 'read', 'args': {'path': 'a.py'}}"
        parsed = parse_response(raw, known_tools=TOOLS)
        assert [c.name for c in parsed.calls] == ["read"]


class TestStreamParserFinish:
    def test_finish_applies_the_same_fallback(self):
        parser = StreamParser()
        parser.feed(json.dumps({"name": "read", "args": {"path": "a.py"}}))
        result = parser.finish(known_tools=TOOLS)
        assert [c.name for c in result.calls] == ["read"]
        assert result.text == ""
