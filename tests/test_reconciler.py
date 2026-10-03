import shutil
import tempfile
import time

import pytest

from xli.core.layers import LayerStore
from xli.core.reconciler import Reconciler, IdleReconciler


@pytest.fixture
def store():
    tmp = tempfile.mkdtemp()
    yield LayerStore(root=tmp)
    shutil.rmtree(tmp, ignore_errors=True)


def test_verify_all_clean_store(store):
    store.commit("a", note="n", kind="code", agent="X")
    report = Reconciler(store).verify_all()
    assert report.clean
    assert report.checked == 1


def test_find_best_prefers_working_tag_over_recency(store):
    prev = None
    for i in range(3):
        prev = store.commit(f"v{i}", note=f"attempt {i}", kind="code", agent="CODER", parent_id=prev)
    working_layer = prev
    store.add_tag(working_layer, "working")
    # a newer, unrelated layer that is NOT tagged working
    store.commit("scratch", note="unrelated later note", kind="note", agent="X")

    r = Reconciler(store)
    assert r.find_best() == working_layer


def test_find_best_falls_back_to_last_good_ref(store):
    layer = store.commit("v1", note="n", kind="code", agent="CODER")
    store.set_ref("last_good", layer)
    r = Reconciler(store)
    assert r.find_best() == layer


def test_consolidate_archives_old_unreferenced_layers_but_keeps_them_readable(store):
    for i in range(10):
        store.commit(f"scratch {i}", note=f"note {i}", kind="note", agent="X")
    r = Reconciler(store)
    report = r.consolidate(keep_recent=2)
    assert len(report.archived) == 8

    archived_id = report.archived[0]
    result = r.get_full_anywhere(archived_id)
    assert result["content"] is not None
    assert result["verified"] is True


def test_store_get_full_finds_archived_content_directly(store):
    """After the archive/objects split moved into LayerStore itself, plain
    store.get_full() (not just the Reconciler alias) must find archived
    content without callers needing to know it moved."""
    for i in range(5):
        store.commit(f"scratch {i}", note=f"note {i}", kind="note", agent="X")
    r = Reconciler(store)
    report = r.consolidate(keep_recent=1)
    archived_id = report.archived[0]

    result = store.get_full(archived_id)
    assert result["content"] is not None
    assert result["verified"] is True


def test_verify_all_does_not_flag_archived_layers_as_missing(store):
    for i in range(5):
        store.commit(f"scratch {i}", note=f"note {i}", kind="note", agent="X")
    r = Reconciler(store)
    r.consolidate(keep_recent=1)
    report = r.verify_all()
    assert report.clean


def test_consolidate_is_idempotent(store):
    for i in range(5):
        store.commit(f"scratch {i}", note=f"n{i}", kind="note", agent="X")
    r = Reconciler(store)
    r.consolidate(keep_recent=1)
    second_pass = r.consolidate(keep_recent=1)
    assert second_pass.archived == []


def test_consolidate_never_archives_working_or_ref_layers(store):
    layer = store.commit("important", note="n", kind="code", agent="CODER")
    store.add_tag(layer, "working")
    for i in range(20):
        store.commit(f"scratch {i}", note=f"note {i}", kind="note", agent="X")

    r = Reconciler(store)
    report = r.consolidate(keep_recent=1)
    assert layer not in report.archived


def test_idle_reconciler_gates_on_threshold(store):
    idle = IdleReconciler(store, idle_threshold_seconds=0.3)
    assert idle.maybe_run() is None  # no time has passed yet
    time.sleep(0.4)
    assert idle.maybe_run() is not None
    assert idle.maybe_run() is None  # no new activity since last run


def test_idle_reconciler_reruns_after_fresh_activity(store):
    idle = IdleReconciler(store, idle_threshold_seconds=0.2)
    time.sleep(0.3)
    assert idle.maybe_run() is not None
    idle.note_activity()
    time.sleep(0.3)
    assert idle.maybe_run() is not None


def test_checkout_best_restores_the_working_version(store, tmp_path):
    target = str(tmp_path / "calc.py")
    a = store.commit("v1", note="buggy", kind="code", agent="CODER",
                      file_writes={target: "def add(a, b):\n    return a - b\n"})
    b = store.commit("v2", note="fixed", kind="code", agent="DEBUGGER", parent_id=a,
                      file_writes={target: "def add(a, b):\n    return a + b\n"})
    store.add_tag(b, "working")

    # simulate external corruption of the live file
    with open(target, "w") as f:
        f.write("GARBAGE")

    r = Reconciler(store)
    result = r.checkout_best()
    assert result is not None
    assert result["layer_id"] == b
    with open(target) as f:
        assert "return a + b" in f.read()


def test_checkout_best_returns_none_for_empty_store(store):
    r = Reconciler(store)
    assert r.checkout_best() is None


def test_checkout_best_respects_dry_run(store, tmp_path):
    target = str(tmp_path / "calc.py")
    layer = store.commit("v1", note="n", kind="code", agent="CODER",
                          file_writes={target: "content"})
    store.add_tag(layer, "working")

    r = Reconciler(store)
    dest = tmp_path / "checkout_preview"
    result = r.checkout_best(dest_root=str(dest), dry_run=True)
    assert result is not None
    assert not dest.exists()
