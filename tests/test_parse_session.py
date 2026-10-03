#!/usr/bin/env python3
"""Tests for the model-output parser and the session store."""

import json

import pytest

from xli.parse import (
    StreamParser,
    ToolCall,
    extract_tool_payloads,
    json_spans,
    loads_lenient,
    parse_response,
    render_call,
    strip_fences,
)
from xli.session import Session, estimate_tokens, session_root


def tool_block(payload: dict) -> str:
    return "<tool>" + json.dumps(payload) + "</tool>"


# ------------------------------------------------------------------ json spans
class TestJsonSpans:
    def test_single_object(self):
        assert json_spans('{"a": 1}') == [(0, 8)]

    def test_nested_braces_do_not_end_the_span(self):
        text = '{"content": "{\\"x\\": 1}"}'
        spans = json_spans(text)
        assert len(spans) == 1
        assert text[spans[0][0] : spans[0][1]] == text

    def test_braces_inside_strings_ignored(self):
        text = '{"a": "}{"}'
        assert len(json_spans(text)) == 1

    def test_multiple_objects(self):
        assert len(json_spans('{"a":1} and {"b":2}')) == 2

    def test_unbalanced_leaves_no_span(self):
        assert json_spans('{"a": 1') == []


class TestLenientJson:
    def test_valid_json_needs_no_repair(self):
        value, repairs = loads_lenient('{"a": 1}')
        assert value == {"a": 1}
        assert repairs == []

    def test_windows_path_backslashes_repaired(self):
        value, repairs = loads_lenient(r'{"path": "C:\Users\me\a.py"}')
        assert value["path"] == "C:\\Users\\me\\a.py"
        assert "backslash" in repairs[0]

    def test_trailing_comma_repaired(self):
        value, repairs = loads_lenient('{"a": 1, "b": 2,}')
        assert value == {"a": 1, "b": 2}
        assert any("trailing comma" in r for r in repairs)

    def test_single_quotes_repaired(self):
        value, repairs = loads_lenient("{'a': 'b'}")
        assert value == {"a": "b"}
        assert repairs

    def test_hopeless_payload_raises(self):
        with pytest.raises(json.JSONDecodeError):
            loads_lenient("not json at all {{{")


class TestStripFences:
    def test_removes_json_fence(self):
        stripped, repaired = strip_fences('```json\n{"a":1}\n```')
        assert stripped.strip() == '{"a":1}'
        assert repaired is False

    def test_unterminated_fence_reported(self):
        stripped, repaired = strip_fences('```json\n{"a":1}')
        assert repaired is True
        assert "```" not in stripped

    def test_plain_text_untouched(self):
        stripped, repaired = strip_fences("just prose")
        assert stripped == "just prose"
        assert repaired is False


# --------------------------------------------------------------------- parsing
class TestParseResponse:
    def test_plain_text_no_calls(self):
        result = parse_response("I will look at that now.")
        assert result.text == "I will look at that now."
        assert not result.has_calls
        assert not result.done

    def test_single_tool_call(self):
        result = parse_response("Reading it.\n" + tool_block({"name": "read", "args": {"path": "a.py"}}))
        assert len(result.calls) == 1
        assert result.calls[0].name == "read"
        assert result.calls[0].args == {"path": "a.py"}
        assert result.text == "Reading it."

    def test_multiple_tool_calls_in_order(self):
        text = tool_block({"name": "a", "args": {}}) + "\n" + tool_block({"name": "b", "args": {}})
        result = parse_response(text)
        assert [c.name for c in result.calls] == ["a", "b"]

    def test_nested_braces_in_args_survive(self):
        payload = {"name": "write", "args": {"path": "a.py", "content": "def f():\n    return {'k': 1}\n"}}
        result = parse_response(tool_block(payload))
        assert result.calls[0].args["content"] == "def f():\n    return {'k': 1}\n"

    def test_done_marker(self):
        result = parse_response("all finished <done>fixed the bug</done>")
        assert result.done is True
        assert result.done_text == "fixed the bug"
        assert result.text == "all finished"

    def test_unterminated_done_still_counts(self):
        result = parse_response("<done>done but no close tag")
        assert result.done is True
        assert "unterminated" in " ".join(result.repairs)

    def test_unparseable_tool_block_is_skipped_with_note(self):
        result = parse_response("<tool>total garbage</tool>")
        assert not result.has_calls
        assert any("unparseable" in r for r in result.repairs)

    def test_prose_inside_tool_tag_recovered(self):
        result = parse_response('<tool>here you go: {"name":"read","args":{"path":"a"}} cheers</tool>')
        assert result.calls[0].name == "read"

    def test_empty_tool_block_noted(self):
        result = parse_response("<tool></tool>")
        assert not result.has_calls
        assert any("empty" in r for r in result.repairs)

    def test_tool_block_without_name_noted(self):
        result = parse_response('<tool>{"args": {}}</tool>')
        assert not result.has_calls
        assert any("no 'name'" in r for r in result.repairs)

    def test_alternative_key_names_accepted(self):
        result = parse_response('<tool>{"tool":"read","arguments":{"path":"a"}}</tool>')
        assert result.calls[0].name == "read"
        assert result.calls[0].args == {"path": "a"}

    def test_non_object_args_coerced(self):
        result = parse_response('<tool>{"name":"read","args":"a.py"}</tool>')
        assert result.calls[0].args == {}
        assert any("coerced" in r for r in result.repairs)

    def test_call_inside_fence(self):
        result = parse_response("```json\n" + tool_block({"name": "read", "args": {}}) + "\n```")
        assert result.calls[0].name == "read"

    def test_to_dict_roundtrip(self):
        result = parse_response(tool_block({"name": "ls", "args": {}}))
        payload = result.to_dict()
        assert payload["calls"][0]["name"] == "ls"


class TestExtractToolPayloads:
    def test_unterminated_block_returns_remainder(self):
        spans = extract_tool_payloads('<tool>{"name":"x"')
        assert len(spans) == 1
        assert spans[0][0].startswith('{"name"')

    def test_two_blocks(self):
        assert len(extract_tool_payloads("<tool>a</tool><tool>b</tool>")) == 2


# ------------------------------------------------------------------- streaming
class TestStreamParser:
    def test_call_emitted_only_when_closed(self):
        parser = StreamParser()
        assert parser.feed('<tool>{"name":"read",') == []
        assert parser.feed('"args":{"path":"a"}}') == []
        calls = parser.feed("</tool>")
        assert len(calls) == 1
        assert calls[0].name == "read"

    def test_no_duplicate_emission(self):
        parser = StreamParser()
        parser.feed(tool_block({"name": "a", "args": {}}))
        assert parser.feed(" more text") == []
        assert parser.finish().calls == []

    def test_two_calls_across_chunks(self):
        parser = StreamParser()
        whole = tool_block({"name": "a", "args": {}}) + tool_block({"name": "b", "args": {}})
        seen = []
        for i in range(0, len(whole), 7):
            seen.extend(parser.feed(whole[i : i + 7]))
        assert [c.name for c in seen] == ["a", "b"]

    def test_pending_strips_completed_blocks(self):
        parser = StreamParser()
        parser.feed("thinking " + tool_block({"name": "a", "args": {}}) + " still here")
        assert "thinking" in parser.pending
        assert "still here" in parser.pending
        assert "<tool>" not in parser.pending

    def test_finish_returns_done_marker(self):
        parser = StreamParser()
        parser.feed("<done>all good</done>")
        result = parser.finish()
        assert result.done and result.done_text == "all good"

    def test_reset(self):
        parser = StreamParser()
        parser.feed(tool_block({"name": "a", "args": {}}))
        parser.reset()
        assert parser.pending == ""


class TestRenderCall:
    def test_roundtrip(self):
        call = ToolCall(name="read", args={"path": "a.py"})
        parsed = parse_response(render_call(call))
        assert parsed.calls[0].name == "read"
        assert parsed.calls[0].args == {"path": "a.py"}


# ------------------------------------------------------------------- sessions
class TestSession:
    def test_append_and_reload(self, tmp_path):
        session = Session(root=tmp_path)
        session.add_user("fix the bug")
        session.add_assistant("on it", calls=[{"name": "read", "args": {"path": "a"}}])
        session.add_tool("read", "file contents", ok=True)

        reloaded = Session.load(session.session_id, root=tmp_path)
        # meta header + user + assistant + tool
        assert len(reloaded) == 4
        assert reloaded.events[0].kind == "meta"
        assert reloaded.events[1].data["content"] == "fix the bug"

    def test_file_is_jsonl_one_event_per_line(self, tmp_path):
        session = Session(root=tmp_path)
        session.add_user("one")
        session.add_user("two")
        lines = session.path.read_text().strip().splitlines()
        assert len(lines) == 3  # meta header + two events
        assert all(json.loads(line) for line in lines)
        assert json.loads(lines[0])["kind"] == "meta"

    def test_messages_shape(self, tmp_path):
        session = Session(root=tmp_path)
        session.add_user("hello")
        session.add_assistant("hi there")
        session.add_tool("ls", "a.py b.py")
        messages = session.messages()
        assert messages[0] == {"role": "user", "content": "hello"}
        assert messages[1] == {"role": "assistant", "content": "hi there"}
        assert "[tool ls: ok]" in messages[2]["content"]

    def test_failed_tool_marked_as_error(self, tmp_path):
        session = Session(root=tmp_path)
        session.add_tool("bash", "boom", ok=False)
        assert "[tool bash: error]" in session.messages()[0]["content"]

    def test_truncation_keeps_first_prompt(self, tmp_path):
        session = Session(root=tmp_path)
        session.add_user("ORIGINAL TASK")
        for i in range(40):
            session.add_user(f"filler message number {i} " * 20)
            session.add_assistant(f"reply {i} " * 20)
        messages = session.messages(max_tokens=500)
        assert messages[0]["content"] == "ORIGINAL TASK"
        assert sum(estimate_tokens(m["content"]) for m in messages) <= 500 or len(messages) == 2

    def test_list_sessions_newest_first(self, tmp_path):
        first = Session(root=tmp_path)
        first.add_user("first prompt")
        second = Session(root=tmp_path, session_id="zzz-later")
        second.add_user("second prompt")

        listing = Session.list_sessions(root=tmp_path)
        assert [s["id"] for s in listing][0] == "zzz-later"
        assert listing[0]["first_prompt"] == "second prompt"
        assert listing[0]["events"] == 2  # meta header + the prompt
        assert "mtime_ns" not in listing[0], "sort key must not leak into the summary"

    def test_list_sessions_empty(self, tmp_path):
        assert Session.list_sessions(root=tmp_path) == []

    def test_load_missing_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            Session.load("nope", root=tmp_path)

    def test_latest(self, tmp_path):
        session = Session(root=tmp_path, session_id="only-one")
        session.add_user("hi")
        assert Session.latest(root=tmp_path).session_id == "only-one"

    def test_latest_none_when_empty(self, tmp_path):
        assert Session.latest(root=tmp_path) is None

    def test_torn_last_line_is_skipped(self, tmp_path):
        session = Session(root=tmp_path)
        session.add_user("good line")
        with open(session.path, "a", encoding="utf-8") as handle:
            handle.write('{"kind": "user", "content": "torn')
        reloaded = Session.load(session.session_id, root=tmp_path)
        assert len(reloaded) == 2  # meta header + the good line

    def test_delete(self, tmp_path):
        session = Session(root=tmp_path)
        session.add_user("bye")
        assert session.delete() is True
        assert not session.path.exists()

    def test_transcript_readable(self, tmp_path):
        session = Session(root=tmp_path)
        session.add_user("do the thing")
        session.add_assistant("doing", calls=[{"name": "read", "args": {"path": "a"}}])
        session.add_tool("read", "ok")
        text = session.transcript()
        assert "you: do the thing" in text
        assert "-> read" in text

    def test_token_estimate_grows(self, tmp_path):
        session = Session(root=tmp_path)
        assert session.token_estimate == 0
        session.add_user("hello world")
        assert session.token_estimate > 0

    def test_notes_recorded(self, tmp_path):
        session = Session(root=tmp_path)
        session.add_note("permission refused", tool="bash")
        note = session.events[-1]  # events[0] is the meta header
        assert note.data["tool"] == "bash"
        assert note.kind == "note"


class TestTokenEstimate:
    def test_empty(self):
        assert estimate_tokens("") == 0

    def test_ascii(self):
        assert estimate_tokens("a" * 400) == 100

    def test_cjk_costs_more(self):
        assert estimate_tokens("中" * 100) > estimate_tokens("a" * 100)


class TestSessionRoot:
    def test_default_uses_cwd(self):
        assert session_root().parts[-2:] == (".xli", "sessions")

    def test_explicit_root(self, tmp_path):
        assert session_root(tmp_path) == tmp_path / ".xli" / "sessions"
