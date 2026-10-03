#!/usr/bin/env python3
"""
XLI Reconciler — runs when the agent is idle (or on demand), not during an
active task. Two jobs:

  1. Verify — walk every layer in the store, re-hash its content file, and
     flag any where the note/index no longer matches what's actually on
     disk. This is the same check get_full() does for a single layer,
     done in bulk, so drift gets caught even for layers nobody happens to
     read during normal operation.

  2. Consolidate — decide which version is actually "the best one" (most
     recent layer tagged "working", i.e. the one whose tests genuinely
     passed) and archive full content for old, unreferenced, non-working
     layers so the hot object store doesn't grow forever. Archiving moves
     the file, it does not delete it — the note stays in the index either
     way, so history/diff/context_window keep working; get_full() itself
     checks both the hot and archived location (see layers.py), so nothing
     here needs its own archive-aware read path.

Designed to be driven by whatever "idle" means in a given deployment: a
cron job, a call at the end of a UI session, a background thread that wakes
up after N seconds of no activity. IdleReconciler.note_activity() /
maybe_run() give a minimal, dependency-free way to do the latter.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path

from xli.core.layers import LayerStore
from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.reconciler")


@dataclass
class VerifyReport:
    checked: int = 0
    drifted: list[str] = field(default_factory=list)     # layer_ids whose hash no longer matches
    missing: list[str] = field(default_factory=list)      # layer_ids whose content file is gone

    @property
    def clean(self) -> bool:
        return not self.drifted and not self.missing

    def summary(self) -> str:
        if self.clean:
            return f"OK — {self.checked} layers verified, no drift"
        return (
            f"{self.checked} layers checked: {len(self.drifted)} drifted, "
            f"{len(self.missing)} missing content — {self.drifted[:5]}{self.missing[:5]}"
        )


@dataclass
class ConsolidateReport:
    archived: list[str] = field(default_factory=list)
    best_layer: str | None = None
    kept_hot: int = 0


class Reconciler:
    def __init__(self, store: LayerStore):
        self.store = store

    # ---- verification --------------------------------------------------

    def verify_all(self) -> VerifyReport:
        """get_full() already checks archive/ as a fallback, so a layer
        whose content got consolidated away from objects/ isn't reported as
        missing here — 'missing' means genuinely gone from both locations."""
        report = VerifyReport()
        for meta in self.store.all_layers():
            report.checked += 1
            result = self.store.get_full(meta.layer_id)
            if result["content"] is None:
                report.missing.append(meta.layer_id)
            elif not result["verified"]:
                report.drifted.append(meta.layer_id)
        logger.log_structured("INFO", "reconciler", "Verify pass complete", {
            "checked": report.checked, "drifted": len(report.drifted), "missing": len(report.missing),
        })
        return report

    # ---- picking the best version ---------------------------------------

    def find_best(self) -> str | None:
        """The most recent layer tagged 'working' — i.e. the newest code
        version that actually passed its tests, not just the newest version.
        Falls back to the 'last_good' ref, then to HEAD, if no tagged layer
        exists (e.g. nothing has gone through the self-correcting loop yet)."""
        working = self.store.find_by_tag("working")
        if working:
            return working[0].layer_id
        ref = self.store.get_ref("last_good")
        if ref:
            return ref
        return self.store.get_ref("HEAD")

    # ---- consolidation / archiving ---------------------------------------

    def consolidate(self, keep_recent: int = 20, keep_tags: list[str] | None = None) -> ConsolidateReport:
        """Archive full content for layers that are: not among the
        `keep_recent` most recent, not pointed to by any ref, and not tagged
        with anything in `keep_tags` (defaults to 'working'). The layer stays
        in the index (note, history, diffs-by-id-lookup-of-archived-content
        all keep working) — only its hot object file moves under archive/,
        via LayerStore.archive_object() (locked, see layers.py).
        """
        keep_tags = keep_tags or ["working"]
        report = ConsolidateReport()
        report.best_layer = self.find_best()

        all_meta = sorted(self.store.all_layers(), key=lambda m: m.created_at, reverse=True)
        recent_ids = {m.layer_id for m in all_meta[:keep_recent]}
        ref_ids = set(self.store.list_refs().values())

        for meta in all_meta:
            protected = (
                meta.layer_id in recent_ids
                or meta.layer_id in ref_ids
                or any(t in keep_tags for t in meta.tags)
            )
            if protected:
                if Path(meta.content_path).exists():
                    report.kept_hot += 1
                continue
            if self.store.archive_object(meta.layer_id):
                report.archived.append(meta.layer_id)
            elif Path(meta.content_path).exists():
                report.kept_hot += 1

        logger.log_structured("INFO", "reconciler", "Consolidate pass complete", {
            "archived": len(report.archived), "kept_hot": report.kept_hot, "best": report.best_layer,
        })
        return report

    # ---- restoring the best known version --------------------------------

    def checkout_best(self, dest_root: str | None = None, dry_run: bool = False) -> dict | None:
        """Restore project files to the state of find_best() — the newest
        layer actually tagged 'working', or the best available fallback.
        Returns None if there's nothing to check out yet (empty store)."""
        best = self.find_best()
        if best is None:
            return None
        return self.store.checkout(best, dest_root=dest_root, dry_run=dry_run)

    def get_full_anywhere(self, layer_id: str):
        """Kept as a thin alias for existing callers — store.get_full() now
        does exactly this (checks archive/ itself) since the archive/ vs
        objects/ split moved into LayerStore. New code should just call
        store.get_full() directly."""
        return self.store.get_full(layer_id)


class IdleReconciler:
    """Minimal activity-gated trigger: call note_activity() whenever the agent
    does real work, call maybe_run() periodically (e.g. from a UI's event
    loop or a scheduled task) — it only actually reconciles once the gap
    since the last activity exceeds idle_threshold_seconds, and won't run
    again until there's been fresh activity after that."""

    def __init__(self, store: LayerStore, idle_threshold_seconds: int = 300):
        self.reconciler = Reconciler(store)
        self.idle_threshold_seconds = idle_threshold_seconds
        self._last_activity = time.time()
        self._last_run: float | None = None

    def note_activity(self):
        self._last_activity = time.time()

    def maybe_run(self) -> dict | None:
        idle_for = time.time() - self._last_activity
        if idle_for < self.idle_threshold_seconds:
            return None
        if self._last_run and self._last_run > self._last_activity:
            return None  # already reconciled since the last bit of activity
        verify_report = self.reconciler.verify_all()
        consolidate_report = self.reconciler.consolidate()
        self._last_run = time.time()
        logger.log_structured("INFO", "reconciler", "Idle reconciliation ran", {
            "idle_for_seconds": round(idle_for), "verify": verify_report.summary(),
        })
        return {"verify": verify_report, "consolidate": consolidate_report}
