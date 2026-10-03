#!/usr/bin/env python3
"""
XLI Self-Correcting Chain — CODER -> TESTER -> DEBUGGER loop that actually
runs the tests, actually reads the failures, and actually retries, instead of
running each agent once and hoping for the best.

How it uses layers.py / goal.py:

  - Every code version CODER/DEBUGGER produces is committed as a "code"
    layer, parented to the previous one — so you always have the real
    history of what changed between attempts, diffable on demand.
  - Every pytest run is committed as a "test_result" layer: the *note* is a
    one-line summary ("3 failed, 5 passed"), the *full* content is the
    complete pytest output. DEBUGGER is fed the note + a diff against the
    last good version by default — it only pulls the full traceback via
    get_full() if that's not enough, which is the "don't hold everything in
    memory, hold notes and check the full file when needed" behavior.
  - Goal.criteria is a single checkable fact for this loop: "pytest exits 0
    for this test file." The loop stops as soon as that's true, or after
    Goal.max_iterations attempts, whichever comes first.
  - refs["last_good"] in the LayerStore always points at the last code layer
    that actually passed its tests — so even mid-loop, "what's the best
    known version so far" is a single lookup, not something to reconstruct
    from the conversation.
"""

from pathlib import Path
from dataclasses import dataclass

from xli.core.chain import XliAgent
from xli.core.layers import LayerStore, get_layer_store
from xli.core.goal import Goal, Criterion, GoalStatus
from xli.core.exec_guard import run_guarded_shell
from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.self_correct")


@dataclass
class RunResult:
    goal: Goal
    layer_store: LayerStore
    final_code_layer: str | None
    passed: bool


def _run_pytest(test_path: str, timeout: int = 60):
    """Actually execute pytest — this is the 'sam проверяет, тестит, сам фиксит'
    part. Goes through exec_guard, same hardening as every other shell call."""
    try:
        result = run_guarded_shell(f"python3 -m pytest {test_path} -v --tb=short", timeout=timeout)
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        passed = result.returncode == 0
    except Exception as e:
        output = f"pytest failed to run: {e}"
        passed = False
    return passed, output


def _summarize_pytest(output: str, passed: bool) -> str:
    """The 'note' for a test_result layer — compact, meant for the model."""
    if passed:
        return "pytest: all tests passed"
    # pull out the last few meaningful lines (failure summary) instead of the
    # whole traceback — that's what belongs in a note, not the full content.
    lines = [l for l in output.splitlines() if l.strip()]
    tail = lines[-6:] if len(lines) > 6 else lines
    return "pytest: FAILED — " + " | ".join(tail)[:400]


class SelfCorrectingChain:
    def __init__(self, coder: XliAgent, debugger: XliAgent,
                 layer_store: LayerStore | None = None, max_iterations: int = 5):
        self.coder = coder
        self.debugger = debugger
        self.store = layer_store or get_layer_store()
        self.max_iterations = max_iterations

    async def run(self, task: str, target_file: str, test_file: str) -> RunResult:
        """task: what to build. target_file: where the implementation goes.
        test_file: pytest file to run against it (assumed to already exist
        or be created by CODER as part of `task`)."""

        goal = Goal(
            description=f"Make {test_file} pass for {target_file}",
            criteria=[Criterion(f"pytest {test_file} exits 0", check=lambda: False)],  # replaced per-iteration below
            max_iterations=self.max_iterations,
        )
        goal.status = GoalStatus.IN_PROGRESS

        parent_layer = self.store.get_ref("HEAD")
        error_context = ""
        final_code_layer = None
        passed = False

        while goal.should_continue():
            iteration = len(goal.attempts) + 1
            logger.log_structured("INFO", "self_correct", f"Iteration {iteration}/{self.max_iterations}")

            # 1) CODER (first pass) or DEBUGGER (subsequent passes) produces code
            agent = self.coder if iteration == 1 else self.debugger
            context = ""
            if parent_layer:
                context = self.store.context_window(from_id=parent_layer, limit=5)

            response = await agent.think(task, context=context, error_context=error_context)

            # Snapshot target_file's actual on-disk content (if it exists after
            # this attempt) as this layer's file_write — ground truth for
            # checkout(), rather than trying to parse it back out of
            # `response`, which may be tool-call text/commentary rather than
            # the raw file content itself.
            file_writes = {}
            try:
                target_path = Path(target_file)
                if target_path.exists():
                    file_writes[str(target_path.resolve())] = target_path.read_text()
            except Exception:
                pass

            code_layer = self.store.commit(
                content=response,
                note=f"{agent.name} attempt {iteration}: {response[:120]!r}",
                kind="code",
                agent=agent.name,
                parent_id=parent_layer,
                file_writes=file_writes,
            )
            parent_layer = code_layer

            # 2) TESTER — actually run pytest, not just ask an LLM if it looks right
            test_passed, test_output = _run_pytest(test_file)
            test_note = _summarize_pytest(test_output, test_passed)
            test_layer = self.store.commit(
                content=test_output,
                note=test_note,
                kind="test_result",
                agent="TESTER",
                parent_id=code_layer,
                tags=["passed"] if test_passed else ["failed"],
            )

            goal.criteria[0].check = lambda p=test_passed: p
            goal.evaluate()
            goal.record_attempt(layer_id=test_layer, note=test_note)

            if test_passed:
                self.store.set_ref("last_good", code_layer)
                self.store.add_tag(code_layer, "working")
                goal.status = GoalStatus.DONE
                final_code_layer = code_layer
                passed = True
                logger.log_structured("INFO", "self_correct", f"Goal met on iteration {iteration}")
                break
            else:
                # Feed DEBUGGER the note + diff against last known good, not the
                # full raw traceback by default — full is one get_full() away.
                last_good = self.store.get_ref("last_good")
                diff_text = ""
                if last_good and last_good != code_layer:
                    try:
                        diff_text = self.store.diff(last_good, code_layer)
                    except Exception:
                        diff_text = ""
                error_context = f"Previous attempt's tests failed.\n{test_note}\n\nDiff vs last good version:\n{diff_text[:1000]}"

        if goal.status != GoalStatus.DONE:
            goal.status = GoalStatus.FAILED
            logger.log_structured("WARN", "self_correct", "Goal not met within max_iterations")

        return RunResult(goal=goal, layer_store=self.store, final_code_layer=final_code_layer, passed=passed)
