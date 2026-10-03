import shutil
import tempfile
import pytest

from xli.core.chain import XliAgent
from xli.core.layers import LayerStore
from xli.providers.fake import FakeProvider
import xli.core.self_correcting_chain as scc


@pytest.fixture
def workdir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _make_agent(name, write_target, content):
    def response_fn(messages):
        with open(write_target, "w") as f:
            f.write(content)
        return f"wrote {write_target}"
    return XliAgent(name, f"ag_{name.lower()}", f"You are {name}.", provider=FakeProvider(response_fn=response_fn))


def _exec_based_checker(target):
    """Stand-in for pytest that avoids subprocess/pyc-cache pitfalls in-process."""
    def _run(test_path, timeout=60):
        ns = {}
        try:
            exec(open(target).read(), ns)
            ok = ns["add"](2, 3) == 5
            return ok, "1 passed" if ok else "1 failed"
        except Exception as e:
            return False, f"exec error: {e}"
    return _run


def test_self_correcting_chain_fixes_bug_and_stops_early(workdir, monkeypatch):
    target = f"{workdir}/calc.py"
    coder = _make_agent("CODER", target, "def add(a, b):\n    return a - b\n")   # buggy
    debugger = _make_agent("DEBUGGER", target, "def add(a, b):\n    return a + b\n")  # fixed

    monkeypatch.setattr(scc, "_run_pytest", _exec_based_checker(target))

    store = LayerStore(root=f"{workdir}/layers")
    chain = scc.SelfCorrectingChain(coder, debugger, layer_store=store, max_iterations=4)

    import asyncio
    result = asyncio.run(chain.run(task="implement add", target_file=target, test_file="unused.py"))

    assert result.passed is True
    assert result.goal.status.value == "done"
    assert len(result.goal.attempts) == 2  # stopped as soon as it passed, not at max_iterations

    last_good = store.get_ref("last_good")
    assert last_good == result.final_code_layer
    assert store.get_full(last_good)["verified"] is True


def test_self_correcting_chain_gives_up_after_max_iterations(workdir, monkeypatch):
    target = f"{workdir}/calc.py"
    # both coder and debugger always produce the SAME bug — never fixed
    coder = _make_agent("CODER", target, "def add(a, b):\n    return a - b\n")
    debugger = _make_agent("DEBUGGER", target, "def add(a, b):\n    return a - b\n")

    monkeypatch.setattr(scc, "_run_pytest", _exec_based_checker(target))

    store = LayerStore(root=f"{workdir}/layers")
    chain = scc.SelfCorrectingChain(coder, debugger, layer_store=store, max_iterations=3)

    import asyncio
    result = asyncio.run(chain.run(task="implement add", target_file=target, test_file="unused.py"))

    assert result.passed is False
    assert result.goal.status.value == "failed"
    assert len(result.goal.attempts) == 3
    assert store.get_ref("last_good") is None
