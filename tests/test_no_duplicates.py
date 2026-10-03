#!/usr/bin/env python3
"""No two modules in the package may be copies of each other.

The tree accumulated byte-identical module pairs in two places —
xli/skills/*.py against xli/core and xli/mcp/servers (28 files), then
xli/core/mcp_recommender.py against xli/mcp/recommender.py and
xli/core/mcp_bridge.py against xli/mcp/bridge.py. Each pair was a real
divergence waiting to happen: fix a bug in one copy and the other keeps it.

These tests make the invariant explicit rather than relying on someone
noticing.
"""

import hashlib
from collections import defaultdict
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "xli"


def _modules():
    return [
        p
        for p in PACKAGE.rglob("*.py")
        if "__pycache__" not in str(p) and p.stat().st_size >= 20
    ]


def test_no_byte_identical_modules():
    by_hash = defaultdict(list)
    for path in _modules():
        by_hash[hashlib.sha256(path.read_bytes()).hexdigest()].append(path)

    duplicates = [sorted(str(p) for p in group) for group in by_hash.values() if len(group) > 1]
    assert not duplicates, f"byte-identical module copies: {duplicates}"


def test_no_near_identical_modules():
    """Catches a copy that drifted by a line or two.

    Restricted to same-sized files so this stays cheap; a copy that changed
    length is a genuine fork and worth reviewing by hand.
    """
    import difflib
    from itertools import combinations

    by_size = defaultdict(list)
    for path in _modules():
        by_size[path.stat().st_size].append(path)

    offenders = []
    for size, group in by_size.items():
        if len(group) < 2 or size < 400:
            continue
        for a, b in combinations(sorted(group), 2):
            lines_a = a.read_text(encoding="utf-8", errors="replace").splitlines()
            lines_b = b.read_text(encoding="utf-8", errors="replace").splitlines()
            if difflib.SequenceMatcher(None, lines_a, lines_b).ratio() > 0.95:
                offenders.append((str(a), str(b)))

    assert not offenders, f"near-identical module copies: {offenders}"


def test_the_known_duplicates_are_gone():
    """Pins the specific removals so they cannot quietly come back."""
    for stale in (
        "core/mcp_recommender.py",
        "core/mcp_bridge.py",
    ):
        assert not (PACKAGE / stale).exists(), f"{stale} was removed as a duplicate"


def test_the_surviving_copies_are_the_wired_ones():
    """The copy that remains must be the one something actually imports."""
    for survivor, importer in (
        ("xli/mcp/recommender.py", "xli.kernel.methods"),
        ("xli/mcp/bridge.py", "xli.core.agent"),
    ):
        assert (PACKAGE.parent / survivor).exists()
        source = (PACKAGE.parent / importer.replace(".", "/")).with_suffix(".py").read_text(
            encoding="utf-8"
        )
        module = survivor.rsplit("/", 1)[1][:-3]
        assert module in source, f"{importer} no longer references {survivor}"
