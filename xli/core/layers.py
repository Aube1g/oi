#!/usr/bin/env python3
"""
XLI Layers — git-commit-style context/history layering.

Why this exists: an agent running for a long session either (a) keeps the
full transcript of everything it did — expensive, noisy, drowns the model in
irrelevant detail — or (b) summarizes and throws the original away, which is
cheap but loses information permanently and can't be double-checked later.

LayerStore gives a third option:

  - Every meaningful step (a plan, a code version, a test run, a fix) is
    committed as a *layer*, the same idea as a git commit: it has an id, a
    parent (so you can walk history), and metadata.
  - The *note* (short, human/LLM-written summary — "3 tests failing:
    IndexError in parser.py") is what lives in the cheap index and is what
    gets fed back into an LLM's context. This is what you actually want to
    reason over most of the time.
  - The *full* content (the actual file, the actual traceback, the actual
    diff) is written to disk once and referenced by hash. It is NOT kept in
    memory/context by default.
  - Because the note is a lossy summary, it can drift from what actually
    happened, or simply not be enough. get_full() always re-hashes the file
    on disk against the hash recorded at commit time — if the note's picture
    of the world stops matching reality (or someone edited the file out from
    under the store), that's caught explicitly instead of silently trusting
    a summary that might be wrong.

This is intentionally file-based and dependency-free (stdlib only) so it
works the same whether it's driven by chain.py, multistep.py, or a human
poking at it directly.
"""

import hashlib
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.layers")

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False  # Windows — falls back to no cross-process locking (documented below)


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


def _short_id(content: str, salt: str) -> str:
    return hashlib.sha256(f"{content}{salt}{time.time_ns()}".encode()).hexdigest()[:12]


@dataclass
class LayerMeta:
    """The cheap part — what actually lives in the index and gets kept around
    in an agent's working context. No full content here on purpose."""
    layer_id: str
    parent_id: str | None
    kind: str            # "plan" | "code" | "test_result" | "fix" | "note" | ...
    agent: str            # which agent/role produced this layer
    note: str             # short human/LLM-written summary
    content_hash: str     # sha256 of the full content, for self-verification
    content_path: str     # where the full content lives on disk
    created_at: float
    tags: list[str] = field(default_factory=list)
    # Real project file paths this layer actually wrote, mapped to the exact
    # content written at commit time (absolute paths). Optional and empty by
    # default so old index.json files without this field still load fine —
    # LayerMeta(**v) just leaves it as []. This is what checkout() replays;
    # without it a layer only has the raw agent-response text, which isn't
    # necessarily the same string as what ended up on disk (tool-call
    # envelopes, edits applied on top of existing content, etc).
    file_writes: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LayerStore:
    """One store per project/session. Backed by a directory on disk:

        <root>/index.json       — all LayerMeta, cheap to load fully
        <root>/refs.json        — named pointers (HEAD, last_good, ...)
        <root>/objects/<id>.txt — full content per layer
    """

    def __init__(self, root: str = "~/.xli/layers/default"):
        self.root = Path(os.path.expanduser(root))
        self.objects_dir = self.root / "objects"
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir = self.root / "archive"
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self.refs_path = self.root / "refs.json"
        self._lock_path = self.root / ".lock"
        self._lock_path.touch(exist_ok=True)
        self._index: dict[str, LayerMeta] = self._load_index()
        self._refs: dict[str, str] = self._load_refs()

    # ---- locking --------------------------------------------------------

    @contextmanager
    def _locked(self):
        """Guards every read-modify-write of index.json/refs.json.

        Two things have to both be true for concurrent commit()/set_ref()/
        add_tag() calls (same root, different threads OR different
        processes) to not lose updates:
          1. Only one writer touches the files at a time (the OS file lock).
          2. Whoever's writing reloads from disk first (see _reload()) — a
             lock alone doesn't help if the writer's in-memory self._index
             is already stale from before it acquired the lock; it would
             still overwrite whatever another process committed in the
             meantime. Every mutating method below does lock-then-reload,
             not just lock.

        fcntl.flock is POSIX-only. On Windows this silently degrades to
        in-process-only safety (the lock file is opened but never actually
        locked) — same-process concurrent calls are still fine since Python
        dict mutation + the GIL keep _index/_refs consistent for the
        non-blocking parts, but cross-process races on Windows are not
        covered. Worth a real cross-platform lock (e.g. `portalocker`) if
        this ever needs to run multi-process on Windows.
        """
        if not _HAS_FCNTL:
            yield
            return
        fd = os.open(str(self._lock_path), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _reload(self):
        """Re-read index/refs from disk. Call this AFTER acquiring the lock
        and BEFORE mutating, in every method that writes — see _locked()."""
        self._index = self._load_index()
        self._refs = self._load_refs()

    # ---- persistence -----------------------------------------------------

    def _load_index(self) -> dict[str, LayerMeta]:
        if not self.index_path.exists():
            return {}
        try:
            raw = json.loads(self.index_path.read_text())
            return {k: LayerMeta(**v) for k, v in raw.items()}
        except Exception as e:
            logger.log_structured("ERROR", "layers", "Index load failed, starting fresh", {"error": str(e)})
            return {}

    def _save_index(self):
        payload = {k: v.to_dict() for k, v in self._index.items()}
        self.index_path.write_text(json.dumps(payload, indent=2))

    def _load_refs(self) -> dict[str, str]:
        if not self.refs_path.exists():
            return {}
        try:
            return json.loads(self.refs_path.read_text())
        except Exception:
            return {}

    def _save_refs(self):
        self.refs_path.write_text(json.dumps(self._refs, indent=2))

    # ---- core API ----------------------------------------------------------

    def commit(
        self,
        content: str,
        note: str,
        kind: str,
        agent: str,
        parent_id: str | None = None,
        tags: list[str] | None = None,
        move_head: bool = True,
        file_writes: dict[str, str] | None = None,
    ) -> str:
        """Write a new layer. Returns the layer_id.

        `content` is the full artifact (code, log, plan text) — written once
        to disk. `note` is the compact summary that should be what gets fed
        back into LLM context going forward.

        `file_writes`, if given, is {absolute_project_path: content_written}
        for every real project file this step actually wrote — this is what
        checkout() replays. It's separate from `content` because `content`
        is often the raw agent response (may include tool-call envelopes,
        commentary, etc), not necessarily byte-identical to what landed on
        disk.
        """
        layer_id = _short_id(content, agent)
        content_path = self.objects_dir / f"{layer_id}.txt"
        content_path.write_text(content)

        writes_record: list[dict[str, str]] = []
        if file_writes:
            for path, file_content in file_writes.items():
                obj_path = self.objects_dir / f"{layer_id}__{_hash(path)[:10]}.filewrite"
                obj_path.write_text(file_content)
                writes_record.append({
                    "path": path,
                    "object_path": str(obj_path),
                    "hash": _hash(file_content),
                })

        with self._locked():
            self._reload()
            meta = LayerMeta(
                layer_id=layer_id,
                parent_id=parent_id or self._refs.get("HEAD"),
                kind=kind,
                agent=agent,
                note=note,
                content_hash=_hash(content),
                content_path=str(content_path),
                created_at=time.time(),
                tags=tags or [],
                file_writes=writes_record,
            )
            self._index[layer_id] = meta
            self._save_index()

            if move_head:
                self._refs["HEAD"] = layer_id
                self._save_refs()

        logger.log_structured("INFO", "layers", f"Committed {kind} layer {layer_id}", {"note": note[:80]})
        return layer_id

    def attach_file_writes(self, layer_id: str, file_writes: dict[str, str]):
        """Record real project file writes against an *already-committed*
        layer. Needed because tool calls (write/edit) are currently parsed
        and executed by the caller (chain.py's _process_tools) as a
        separate step after the layer for that agent response is already
        committed — this lets that second step attribute the resulting
        file state back to the right layer without re-committing it.

        Merges with (doesn't replace) any file_writes already on the layer,
        last write for a given path wins, same semantics as commit()."""
        with self._locked():
            self._reload()
            meta = self._index.get(layer_id)
            if meta is None:
                raise KeyError(f"No such layer: {layer_id}")

            existing = {fw["path"]: fw for fw in meta.file_writes}
            for path, content in file_writes.items():
                obj_path = self.objects_dir / f"{layer_id}__{_hash(path)[:10]}.filewrite"
                obj_path.write_text(content)
                existing[path] = {
                    "path": path,
                    "object_path": str(obj_path),
                    "hash": _hash(content),
                }
            meta.file_writes = list(existing.values())
            self._save_index()

    def get_note(self, layer_id: str) -> LayerMeta | None:
        """Cheap lookup — no disk read of the full content."""
        return self._index.get(layer_id)

    def get_full(self, layer_id: str) -> dict[str, Any]:
        """Read the full content, verifying it against the hash recorded at
        commit time. Always returns the content (self-verification informs,
        it doesn't block) but flags `verified: False` if the file drifted.

        Checks the hot objects/ dir first, then archive/ — Reconciler.consolidate()
        moves cold layers' content there without touching content_path in the
        index (updating the index under lock just to rewrite a path felt like
        more moving parts than a two-location lookup), so this is the one
        place that needs to know both locations exist."""
        meta = self._index.get(layer_id)
        if meta is None:
            raise KeyError(f"No such layer: {layer_id}")

        path = Path(meta.content_path)
        if not path.exists():
            archived_path = self.archive_dir / path.name
            if archived_path.exists():
                path = archived_path
            else:
                logger.log_structured("ERROR", "layers", f"Layer {layer_id} content file missing", {"path": str(path)})
                return {"content": None, "verified": False, "note": meta.note, "reason": "content file missing"}

        content = path.read_text()
        actual_hash = _hash(content)
        verified = actual_hash == meta.content_hash
        if not verified:
            logger.log_structured(
                "WARN", "layers",
                f"Layer {layer_id} content hash mismatch — note may no longer describe the actual content",
                {"expected": meta.content_hash[:12], "actual": actual_hash[:12]},
            )
        return {"content": content, "verified": verified, "note": meta.note}

    def verify(self, layer_id: str) -> bool:
        return self.get_full(layer_id)["verified"]

    def archive_object(self, layer_id: str) -> bool:
        """Move a layer's content file from objects/ to archive/, under the
        same lock as every other mutation. Used by Reconciler.consolidate()
        instead of it reaching into LayerStore internals / doing its own
        unlocked shutil.move — the lock matters here because a move race
        against a concurrent get_full() read is low-risk but not zero-risk
        (same-filesystem rename is near-atomic on POSIX, not guaranteed).
        Returns False if there was nothing to move (already archived, or
        never existed) — not an error, just a no-op."""
        with self._locked():
            meta = self._index.get(layer_id)
            if meta is None:
                raise KeyError(f"No such layer: {layer_id}")
            path = Path(meta.content_path)
            if not path.exists():
                return False
            dest = self.archive_dir / path.name
            path.rename(dest)
            return True

    # ---- history / navigation ----------------------------------------------

    def history(self, from_id: str | None = None, limit: int = 50) -> list[LayerMeta]:
        """Walk parent links from `from_id` (default HEAD) back through history.
        Returns notes only — cheap, meant to be handed to an LLM as context."""
        start = from_id or self._refs.get("HEAD")
        chain = []
        seen = set()
        cur = start
        while cur and cur not in seen and len(chain) < limit:
            meta = self._index.get(cur)
            if meta is None:
                break
            chain.append(meta)
            seen.add(cur)
            cur = meta.parent_id
        return chain

    def checkout(
        self,
        layer_id: str,
        dest_root: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Restore real project files to their state as of `layer_id`.

        Walks parent links from `layer_id` back to the root, collecting
        every recorded file_write along the way (oldest first), then
        replays them in order so later writes/edits correctly win over
        earlier ones — this reconstructs "what the files looked like right
        after this layer was committed," not just what this one layer wrote.

        `dest_root`: if given, paths are restored under this directory
        instead of their original absolute location (safe way to inspect a
        past state without touching the live project). If omitted, restores
        to the original recorded paths — this DOES overwrite live files, by
        design (that's what a checkout is), so callers should mean it.

        `dry_run=True` computes and returns what *would* be written without
        touching disk — use this to preview before doing a real checkout.

        Only layers committed with `file_writes` (see LayerStore.commit)
        can be checked out. Layers from before that field existed, or ones
        that never had project files attached (e.g. plain "note" layers),
        contribute nothing and are silently skipped — checkout() reports
        which layers had nothing to restore, it doesn't fail on them.
        """
        chain = list(reversed(self.history(from_id=layer_id, limit=10_000)))  # oldest first

        # path -> (content, hash, source_layer_id), last writer along the
        # chain wins, same as replaying git commits in order.
        resolved: dict[str, dict[str, str]] = {}
        empty_layers: list[str] = []
        for meta in chain:
            if not meta.file_writes:
                empty_layers.append(meta.layer_id)
                continue
            for fw in meta.file_writes:
                resolved[fw["path"]] = {
                    "object_path": fw["object_path"],
                    "hash": fw["hash"],
                    "source_layer": meta.layer_id,
                }

        restored: list[str] = []
        verified_mismatches: list[str] = []
        for path, info in resolved.items():
            obj_path = Path(info["object_path"])
            if not obj_path.exists():
                logger.log_structured("ERROR", "layers", f"checkout: object missing for {path}", {"layer": info["source_layer"]})
                continue
            content = obj_path.read_text()
            if _hash(content) != info["hash"]:
                verified_mismatches.append(path)

            target = Path(dest_root) / Path(path).relative_to(Path(path).anchor) if dest_root else Path(path)
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            restored.append(str(target))

        logger.log_structured(
            "INFO", "layers",
            f"Checkout of {layer_id}: {'(dry run) ' if dry_run else ''}{len(restored)} file(s)",
            {"layer_id": layer_id, "dest_root": dest_root},
        )
        return {
            "layer_id": layer_id,
            "restored_paths": restored,
            "skipped_layers_without_writes": empty_layers,
            "hash_mismatches": verified_mismatches,
            "dry_run": dry_run,
        }

    def diff(self, layer_id_a: str, layer_id_b: str) -> str:
        from xli.core.diff_engine import DiffEngine
        a = self.get_full(layer_id_a)
        b = self.get_full(layer_id_b)
        engine = DiffEngine()
        return engine.unified_diff(a["content"] or "", b["content"] or "", filename=f"{layer_id_a}..{layer_id_b}")

    # ---- refs (named pointers, like git branches/tags) ----------------------

    def set_ref(self, name: str, layer_id: str):
        with self._locked():
            self._reload()
            if layer_id not in self._index:
                raise KeyError(f"No such layer: {layer_id}")
            self._refs[name] = layer_id
            self._save_refs()

    def add_tag(self, layer_id: str, tag: str):
        """Tag an already-committed layer (e.g. mark a code layer 'working'
        once we know its tests passed, after the fact)."""
        with self._locked():
            self._reload()
            meta = self._index.get(layer_id)
            if meta is None:
                raise KeyError(f"No such layer: {layer_id}")
            if tag not in meta.tags:
                meta.tags.append(tag)
                self._save_index()

    def find_by_tag(self, tag: str, kind: str | None = None) -> list[LayerMeta]:
        """All layers carrying a given tag, newest first. Used by the
        reconciler to find e.g. every 'passed' test_result without re-reading
        full content or parsing notes."""
        results = [m for m in self._index.values() if tag in m.tags and (kind is None or m.kind == kind)]
        return sorted(results, key=lambda m: m.created_at, reverse=True)

    def all_layers(self) -> list[LayerMeta]:
        return list(self._index.values())

    def get_ref(self, name: str) -> str | None:
        return self._refs.get(name)

    def list_refs(self) -> dict[str, str]:
        """All named refs (HEAD, last_good, ...) -> layer_id. Public so
        callers like Reconciler don't need to reach into self._refs."""
        return dict(self._refs)

    def context_window(self, from_id: str | None = None, limit: int = 10) -> str:
        """Render recent history as compact notes — this is what you actually
        hand to an LLM, not the full content. Newest first."""
        lines = []
        for meta in self.history(from_id, limit=limit):
            lines.append(f"[{meta.layer_id}] ({meta.kind}/{meta.agent}) {meta.note}")
        return "\n".join(lines) if lines else "(no history yet)"


_default_store: LayerStore | None = None


def get_layer_store(root: str = "~/.xli/layers/default") -> LayerStore:
    global _default_store
    if _default_store is None:
        _default_store = LayerStore(root)
    return _default_store


def reset_layer_store():
    """For tests — drop the cached singleton so a fresh root can be used."""
    global _default_store
    _default_store = None
