def test_chain_init(fake_provider):
    from xli.core.chain import XliCore
    from xli.core.env import EnvironmentAdapter
    core = XliCore(EnvironmentAdapter(), provider=fake_provider)
    assert "CODER" in core.agents


def test_run_chain_completes_all_six_steps_and_commits_layers(tmp_path):
    """The real regression this guards against: run_chain() used to be
    untestable end-to-end and, unknown to anyone, could never actually reach
    the end — two AttributeErrors (self.env.project_dir, self.memory) meant
    it always died partway through the first step. test_chain_agents only
    ever checked that XliCore.agents was populated; it never actually called
    run_chain(). This test calls it for real, on FakeProvider, and checks
    every one of the 6 steps produced a layer and the chain finished."""
    import asyncio
    from xli.core.chain import XliCore
    from xli.core.env import EnvironmentAdapter
    from xli.core.layers import LayerStore
    from xli.providers.fake import FakeProvider

    env = EnvironmentAdapter()
    env.project_dir = str(tmp_path)
    store = LayerStore(root=str(tmp_path / "layers"))

    provider = FakeProvider(responses=["did the step"] * 6)
    core = XliCore(env, provider=provider, layer_store=store)

    result = asyncio.run(core.run_chain("build a thing"))

    # It reached the end and produced output for every step, not just the
    # first one before silently dying.
    assert result.plan and result.coder and result.debugger
    assert result.tester and result.optimizer and result.reviewer
    assert result.final  # only set at the very end of run_chain()

    # Every step actually got committed as a layer — this is what
    # self.memory / self.env.project_dir being missing used to prevent.
    history = store.history(limit=20)
    agents_seen = {m.agent for m in history}
    assert agents_seen == {"PLANNER", "CODER", "DEBUGGER", "TESTER", "OPTIMIZER", "REVIEWER"}
    assert len(history) == 6

    # History is a real walkable parent chain from HEAD back to the first step.
    assert history[0].agent == "REVIEWER"  # most recent
    assert history[-1].agent == "PLANNER"  # oldest
    for child, parent in zip(history, history[1:]):
        assert child.parent_id == parent.layer_id


def test_run_chain_with_self_correction_enabled_still_completes(tmp_path):
    """enable_self_correction=True takes a different path through DEBUGGER
    (a real loop instead of one blind pass) — make sure that path also
    reaches the end rather than only the default flag value being tested."""
    import asyncio
    import json as _json
    from xli.core.chain import XliCore
    from xli.core.env import EnvironmentAdapter
    from xli.core.layers import LayerStore
    from xli.providers.fake import FakeProvider

    env = EnvironmentAdapter()
    env.project_dir = str(tmp_path)
    store = LayerStore(root=str(tmp_path / "layers"))

    test_file = str(tmp_path / "test_thing.py")
    # NOTE: built outside the f-string — Python < 3.12 forbids backslashes in the
    # expression part of an f-string, and the payload needs real newlines.
    write_payload = _json.dumps({
        "name": "write",
        "args": {"path": test_file, "content": "def test_ok():\n    assert True\n"},
    })
    write_test_tool = f"<tool>{write_payload}</tool>"

    responses = [
        "planned it",                    # PLANNER
        "wrote the code",                # CODER
        "debugged it",                   # DEBUGGER (initial pass, before self-correction loop)
        write_test_tool,                 # TESTER — writes a real test_*.py so the self-correction branch triggers
        "coder in loop",                 # SelfCorrectingChain CODER pass (iteration 1)
        "optimized",                     # OPTIMIZER
        "reviewed",                      # REVIEWER
    ]
    provider = FakeProvider(responses=responses)
    core = XliCore(env, provider=provider, layer_store=store)

    result = asyncio.run(core.run_chain("build a thing", enable_self_correction=True, max_correction_iterations=1))

    assert result.final
    assert "[self-correction:" in result.debugger


def _fake_chain_env_and_store(tmp_path, n_responses=6, extra=None):
    from xli.core.env import EnvironmentAdapter
    from xli.core.layers import LayerStore
    from xli.providers.fake import FakeProvider
    env = EnvironmentAdapter()
    env.project_dir = str(tmp_path)
    store = LayerStore(root=str(tmp_path / "layers"))
    responses = ["x"] * n_responses if extra is None else extra
    return env, store, FakeProvider(responses=responses)


def test_default_mode_is_manual_and_self_correction_stays_off(tmp_path):
    """Backward compatibility: XliCore(env) with no mode= argument, and
    run_chain() with no enable_self_correction= argument, must behave
    exactly like before AutonomyMode existed."""
    import asyncio
    from xli.core.chain import XliCore, AutonomyMode

    env, store, provider = _fake_chain_env_and_store(tmp_path)
    core = XliCore(env, provider=provider, layer_store=store)
    assert core.mode == AutonomyMode.MANUAL

    result = asyncio.run(core.run_chain("task"))
    assert "[self-correction:" not in result.debugger


def test_assisted_mode_auto_enables_self_correction(tmp_path):
    import asyncio
    import json as _json
    from xli.core.chain import XliCore, AutonomyMode

    test_file = str(tmp_path / "test_x.py")
    write_payload2 = _json.dumps({
        "name": "write",
        "args": {"path": test_file, "content": "def test_ok():\n    assert True\n"},
    })
    write_test = f"<tool>{write_payload2}</tool>"
    responses = ["plan", "code", "debug", write_test, "coder loop pass", "opt", "rev"]
    env, store, provider = _fake_chain_env_and_store(tmp_path, extra=responses)

    core = XliCore(env, provider=provider, layer_store=store, mode=AutonomyMode.ASSISTED)
    result = asyncio.run(core.run_chain("task", max_correction_iterations=1))

    assert "[self-correction:" in result.debugger
    # ASSISTED does not auto-drive the idle reconciler — only AUTONOMOUS does.
    assert core.reconciler._last_run is None


def test_explicit_enable_self_correction_overrides_mode(tmp_path):
    """Passing enable_self_correction explicitly always wins over whatever
    the mode would otherwise decide — old call sites that already pass a
    bool keep working exactly as before, in any mode."""
    import asyncio
    from xli.core.chain import XliCore, AutonomyMode

    env, store, provider = _fake_chain_env_and_store(tmp_path)
    core = XliCore(env, provider=provider, layer_store=store, mode=AutonomyMode.ASSISTED)
    result = asyncio.run(core.run_chain("task", enable_self_correction=False))
    assert "[self-correction:" not in result.debugger


def test_autonomous_mode_reconciles_on_real_idle_gap_between_calls(tmp_path):
    """The actual point of AUTONOMOUS mode: two run_chain() calls with a
    real gap between them (longer than idle_threshold_seconds) should get
    reconciled automatically, with no cron job or background thread."""
    import asyncio
    import time
    from xli.core.chain import XliCore, AutonomyMode

    env, store, provider = _fake_chain_env_and_store(tmp_path, n_responses=20)
    core = XliCore(env, provider=provider, layer_store=store,
                    mode=AutonomyMode.AUTONOMOUS, idle_threshold_seconds=0.2)

    asyncio.run(core.run_chain("task1"))
    assert core.reconciler._last_run is None  # no real gap yet on the first call

    time.sleep(0.3)  # simulate a real idle gap between tasks
    asyncio.run(core.run_chain("task2"))
    assert core.reconciler._last_run is not None  # the gap got caught and reconciled


def test_invalid_mode_raises():
    import pytest as _pytest
    from xli.core.chain import XliCore
    from xli.core.env import EnvironmentAdapter
    with _pytest.raises(ValueError):
        XliCore(EnvironmentAdapter(), mode="definitely-not-a-real-mode")
