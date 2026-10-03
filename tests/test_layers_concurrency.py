import os
import subprocess
import sys
import textwrap
from pathlib import Path


from xli.core.layers import LayerStore


# Repo root, so the spawned worker can import `xli` even though it lives in a
# tmp dir and pytest's sys.path tweaks do not survive into a fresh interpreter.
REPO_ROOT = str(Path(__file__).resolve().parent.parent)


WORKER_SCRIPT = textwrap.dedent("""
    import sys
    from xli.core.layers import LayerStore
    store_root, worker_id, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
    store = LayerStore(root=store_root)
    for i in range(n):
        store.commit(f"content from {worker_id} #{i}", note=f"{worker_id}-{i}", kind="code", agent=worker_id)
""")


def test_concurrent_processes_dont_lose_commits(tmp_path):
    """Regression test: before the fcntl lock + reload-before-mutate fix,
    N processes committing to the same store root concurrently would lose
    updates — each process's in-memory index was stale relative to the
    others', and _save_index() writes the whole dict, silently dropping
    whatever another process had just committed."""
    script = tmp_path / "worker.py"
    script.write_text(WORKER_SCRIPT)
    store_root = tmp_path / "store"

    n_workers = 5
    n_commits_each = 20

    procs = []
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = REPO_ROOT + (os.pathsep + existing if existing else "")
    for i in range(n_workers):
        p = subprocess.Popen(
            [sys.executable, str(script), str(store_root), f"worker{i}", str(n_commits_each)],
            env=env,
        )
        procs.append(p)
    for p in procs:
        assert p.wait(timeout=30) == 0

    store = LayerStore(root=str(store_root))
    assert len(store.all_layers()) == n_workers * n_commits_each


def test_lock_is_released_after_commit(tmp_path):
    """Sanity check that _locked() doesn't deadlock on repeated use within
    the same process/instance."""
    store = LayerStore(root=str(tmp_path / "store"))
    for i in range(10):
        store.commit(f"v{i}", note=f"n{i}", kind="code", agent="X")
    assert len(store.all_layers()) == 10
