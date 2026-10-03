import shutil
import tempfile
from pathlib import Path

import pytest

from xli.core.layers import LayerStore


@pytest.fixture
def store():
    tmp = tempfile.mkdtemp()
    yield LayerStore(root=tmp)
    shutil.rmtree(tmp, ignore_errors=True)


def test_commit_and_get_note(store):
    layer_id = store.commit("full content here", note="short summary", kind="code", agent="CODER")
    meta = store.get_note(layer_id)
    assert meta.note == "short summary"
    assert meta.kind == "code"


def test_get_full_returns_content_and_verifies(store):
    layer_id = store.commit("hello world", note="n", kind="note", agent="X")
    result = store.get_full(layer_id)
    assert result["content"] == "hello world"
    assert result["verified"] is True


def test_self_verification_catches_external_tamper(store):
    """The core promise: if the note stops matching what's actually on disk,
    get_full() detects it instead of silently trusting the summary."""
    layer_id = store.commit("original content", note="n", kind="note", agent="X")
    meta = store.get_note(layer_id)
    with open(meta.content_path, "a") as f:
        f.write("\ntampered")
    result = store.get_full(layer_id)
    assert result["verified"] is False


def test_history_walks_parent_chain(store):
    a = store.commit("v1", note="first", kind="code", agent="CODER")
    b = store.commit("v2", note="second", kind="code", agent="CODER", parent_id=a)
    c = store.commit("v3", note="third", kind="code", agent="CODER", parent_id=b)
    hist = store.history(from_id=c)
    assert [m.layer_id for m in hist] == [c, b, a]


def test_refs(store):
    layer_id = store.commit("v1", note="n", kind="code", agent="CODER")
    store.set_ref("last_good", layer_id)
    assert store.get_ref("last_good") == layer_id


def test_diff_between_layers(store):
    a = store.commit("line1\nline2\n", note="v1", kind="code", agent="CODER")
    b = store.commit("line1\nline2 changed\n", note="v2", kind="code", agent="CODER", parent_id=a)
    diff_text = store.diff(a, b)
    assert "line2" in diff_text


def test_context_window_is_compact_notes_only(store):
    store.commit("a" * 10000, note="short note", kind="code", agent="CODER")
    window = store.context_window(limit=5)
    assert "short note" in window
    assert "a" * 100 not in window  # full content must not leak into the window


def test_checkout_restores_recorded_file_writes(store, tmp_path):
    target = str(tmp_path / "calc.py")
    layer_id = store.commit(
        "agent response text", note="n", kind="code", agent="CODER",
        file_writes={target: "def add(a, b):\n    return a + b\n"},
    )
    dest = tmp_path / "checkout_here"
    result = store.checkout(layer_id, dest_root=str(dest))
    assert len(result["restored_paths"]) == 1
    restored_content = Path(result["restored_paths"][0]).read_text()
    assert "return a + b" in restored_content
    assert result["hash_mismatches"] == []


def test_checkout_replays_history_last_writer_wins(store, tmp_path):
    target = str(tmp_path / "calc.py")
    a = store.commit("v1", note="buggy", kind="code", agent="CODER",
                      file_writes={target: "def add(a, b):\n    return a - b\n"})
    b = store.commit("v2", note="fixed", kind="code", agent="DEBUGGER", parent_id=a,
                      file_writes={target: "def add(a, b):\n    return a + b\n"})
    c = store.commit("review only, no file writes", note="reviewed", kind="note", agent="REVIEWER", parent_id=b)

    result = store.checkout(c, dest_root=str(tmp_path / "checkout_c"))
    content = Path(result["restored_paths"][0]).read_text()
    assert "return a + b" in content  # inherits latest write from ancestor b, not a
    assert c in result["skipped_layers_without_writes"]


def test_checkout_dry_run_does_not_touch_disk(store, tmp_path):
    target = str(tmp_path / "calc.py")
    layer_id = store.commit("v1", note="n", kind="code", agent="CODER",
                             file_writes={target: "content"})
    dest = tmp_path / "checkout_dry"
    result = store.checkout(layer_id, dest_root=str(dest), dry_run=True)
    assert len(result["restored_paths"]) == 1
    assert not dest.exists()


def test_checkout_without_dest_root_restores_live_path(store, tmp_path):
    target = tmp_path / "calc.py"
    target.write_text("corrupted garbage")
    layer_id = store.commit("v1", note="n", kind="code", agent="CODER",
                             file_writes={str(target): "def add(a, b):\n    return a + b\n"})
    store.checkout(layer_id)  # no dest_root -> restores to original path
    assert "return a + b" in target.read_text()


def test_attach_file_writes_after_commit(store, tmp_path):
    target = str(tmp_path / "out.py")
    layer_id = store.commit("agent response", note="n", kind="agent_step", agent="CODER")
    assert store.get_note(layer_id).file_writes == []

    store.attach_file_writes(layer_id, {target: "print('hi')\n"})
    meta = store.get_note(layer_id)
    assert len(meta.file_writes) == 1
    assert meta.file_writes[0]["path"] == target

    result = store.checkout(layer_id, dest_root=str(tmp_path / "checkout"))
    assert "print('hi')" in Path(result["restored_paths"][0]).read_text()


def test_old_index_without_file_writes_field_loads(tmp_path):
    """Backward compatibility: index.json written before file_writes existed
    should still load, with file_writes defaulting to an empty list."""
    import json
    root = tmp_path / "layers"
    (root / "objects").mkdir(parents=True)
    (root / "objects" / "abc123.txt").write_text("old content")
    old_index = {
        "abc123": {
            "layer_id": "abc123", "parent_id": None, "kind": "note", "agent": "X",
            "note": "old layer", "content_hash": "deadbeef",
            "content_path": str(root / "objects" / "abc123.txt"),
            "created_at": 1.0, "tags": [],
        }
    }
    (root / "index.json").write_text(json.dumps(old_index))

    reloaded = LayerStore(root=str(root))
    meta = reloaded.get_note("abc123")
    assert meta is not None
    assert meta.file_writes == []


def test_archive_object_moves_file_and_get_full_still_finds_it(store):
    layer_id = store.commit("content to archive", note="n", kind="note", agent="X")
    moved = store.archive_object(layer_id)
    assert moved is True

    import os
    assert not os.path.exists(store.get_note(layer_id).content_path)

    result = store.get_full(layer_id)
    assert result["content"] == "content to archive"
    assert result["verified"] is True


def test_archive_object_is_a_noop_when_already_archived(store):
    layer_id = store.commit("v1", note="n", kind="note", agent="X")
    assert store.archive_object(layer_id) is True
    assert store.archive_object(layer_id) is False  # nothing left to move


def test_archive_object_unknown_layer_raises(store):
    import pytest
    with pytest.raises(KeyError):
        store.archive_object("nonexistent")


def test_list_refs_returns_all_named_pointers(store):
    a = store.commit("v1", note="n", kind="code", agent="CODER")
    b = store.commit("v2", note="n", kind="code", agent="CODER", parent_id=a)
    store.set_ref("last_good", a)
    refs = store.list_refs()
    assert refs["last_good"] == a
    assert refs["HEAD"] == b
