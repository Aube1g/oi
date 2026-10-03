#!/usr/bin/env python3
"""
XLI Goal — an explicit, checkable "what am I actually trying to achieve"
object for self-correcting loops.

Without this, a chain of agents just runs a fixed number of steps and stops,
whether or not the actual objective was met. A Goal makes "done" a checkable
fact instead of "we ran out of steps": a list of criteria, each a cheap
callable, checked after every iteration. It deliberately holds almost no
state itself — the *evidence* for whether a criterion passed lives in the
LayerStore (see layers.py) as a layer_id, not duplicated here.
"""

import time
from dataclasses import dataclass, field
from enum import Enum
from collections.abc import Callable

from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.goal")


class GoalStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"  # exhausted iterations without meeting criteria


@dataclass
class Criterion:
    description: str
    check: Callable[[], bool]
    met: bool = False


@dataclass
class Attempt:
    iteration: int
    layer_id: str          # points into LayerStore — the evidence, not duplicated here
    criteria_met: int
    criteria_total: int
    note: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class Goal:
    description: str
    criteria: list[Criterion]
    max_iterations: int = 5
    status: GoalStatus = GoalStatus.PENDING
    attempts: list[Attempt] = field(default_factory=list)

    def evaluate(self) -> bool:
        """Run every criterion's checker. Returns True iff all pass."""
        for c in self.criteria:
            try:
                c.met = bool(c.check())
            except Exception as e:
                logger.log_structured("WARN", "goal", f"Criterion check raised: {c.description}", {"error": str(e)})
                c.met = False
        return all(c.met for c in self.criteria)

    def record_attempt(self, layer_id: str, note: str) -> Attempt:
        met_count = sum(1 for c in self.criteria if c.met)
        attempt = Attempt(
            iteration=len(self.attempts) + 1,
            layer_id=layer_id,
            criteria_met=met_count,
            criteria_total=len(self.criteria),
            note=note,
        )
        self.attempts.append(attempt)
        return attempt

    def should_continue(self) -> bool:
        if self.status in (GoalStatus.DONE, GoalStatus.FAILED):
            return False
        return len(self.attempts) < self.max_iterations

    def summary(self) -> str:
        met = sum(1 for c in self.criteria if c.met)
        return (
            f"Goal: {self.description}\n"
            f"Status: {self.status.value} | Attempts: {len(self.attempts)}/{self.max_iterations} "
            f"| Criteria met: {met}/{len(self.criteria)}\n"
            + "\n".join(f"  [{'x' if c.met else ' '}] {c.description}" for c in self.criteria)
        )
