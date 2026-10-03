#!/usr/bin/env python3
"""
XLI Time Machine v4 — Snapshots, rollback, branch_from snapshot
"""

import json
import shutil
import hashlib
from pathlib import Path
from datetime import datetime

from xli.paths import xli_path
from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.time")

def snapshots_dir() -> Path:
    """Where snapshots live, resolved on every call.

    This used to be a module-level constant, which froze the path at import
    time. Setting XLI_CONFIG_DIR afterwards — as a test does, or as an embedder
    might — was then silently ignored and snapshots were written to the real
    ~/.xli/snapshots. Resolving per call also means the singleton can be pointed
    somewhere else by resetting it.
    """
    return xli_path("snapshots")


class TimeMachine:
    """File snapshot and rollback system"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self._dir = snapshots_dir()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_file = self._dir / "index.json"
        self.snapshots: dict[str, dict] = self._load_index()

        logger.log_structured("INFO", "time",
                             f"TimeMachine initialized, {len(self.snapshots)} snapshots")

    def _load_index(self) -> dict:
        """Load snapshot index"""
        if self._index_file.exists():
            try:
                with open(self._index_file) as f:
                    return json.load(f)
            except Exception as e:
                logger.log_error("time", "Load index failed", exc=e)
        return {}

    def _save_index(self):
        """Save snapshot index"""
        try:
            with open(self._index_file, "w") as f:
                json.dump(self.snapshots, f, indent=2)
        except Exception as e:
            logger.log_error("time", "Save index failed", exc=e)

    def snapshot(self, files: list[str], label: str | None = None) -> str:
        """Create snapshot of files"""
        snapshot_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        if label:
            snapshot_id += f"_{label}"

        snapshot_dir = self._dir / snapshot_id
        snapshot_dir.mkdir(exist_ok=True)

        file_hashes = {}

        for file_path in files:
            path = Path(file_path)
            if not path.exists():
                continue

            # Copy file
            dest = snapshot_dir / path.name
            shutil.copy2(path, dest)

            # Calculate hash
            content = path.read_bytes()
            file_hashes[str(path)] = hashlib.sha256(content).hexdigest()[:16]

        self.snapshots[snapshot_id] = {
            "created": datetime.now().isoformat(),
            "label": label,
            "files": list(file_hashes.keys()),
            "hashes": file_hashes
        }

        self._save_index()
        logger.log_structured("INFO", "time",
                             f"Snapshot created: {snapshot_id}",
                             {"files": len(files)})

        return snapshot_id

    def rollback(self, snapshot_id: str) -> bool:
        """Rollback to snapshot"""
        if snapshot_id not in self.snapshots:
            logger.log_structured("ERROR", "time", f"Snapshot not found: {snapshot_id}")
            return False

        snapshot_dir = self._dir / snapshot_id
        if not snapshot_dir.exists():
            logger.log_structured("ERROR", "time", f"Snapshot dir missing: {snapshot_id}")
            return False

        info = self.snapshots[snapshot_id]

        try:
            for file_path in info["files"]:
                src = snapshot_dir / Path(file_path).name
                if src.exists():
                    shutil.copy2(src, file_path)

            logger.log_structured("INFO", "time",
                                 f"Rolled back to: {snapshot_id}")
            return True

        except Exception as e:
            logger.log_error("time", f"Rollback failed: {snapshot_id}", exc=e)
            return False

    def list_snapshots(self) -> list[dict]:
        """List all snapshots"""
        result = []
        for sid, info in sorted(self.snapshots.items(), reverse=True):
            result.append({
                "id": sid,
                "created": info["created"],
                "label": info.get("label"),
                "files": len(info["files"])
            })
        return result

    def diff_snapshot(self, snapshot_id: str, file_path: str) -> str:
        """Diff current file with snapshot"""
        if snapshot_id not in self.snapshots:
            return "Snapshot not found"

        snapshot_dir = self._dir / snapshot_id
        old_file = snapshot_dir / Path(file_path).name

        if not old_file.exists():
            return "File not in snapshot"

        if not Path(file_path).exists():
            return "Current file not found"

        try:
            old_content = old_file.read_text()
            new_content = Path(file_path).read_text()

            import difflib
            diff = difflib.unified_diff(
                old_content.splitlines(),
                new_content.splitlines(),
                fromfile=f"snapshot/{file_path}",
                tofile=f"current/{file_path}",
                lineterm=""
            )

            return "\n".join(diff)

        except Exception as e:
            logger.log_error("time", "Diff failed", exc=e)
            return f"Error: {e}"

    def branch_from_snapshot(self, snapshot_id: str, branch_name: str) -> bool:
        """Create git branch from snapshot (requires git)"""
        try:
            from xli.core.git import get_git

            # First rollback
            if not self.rollback(snapshot_id):
                return False

            # Then create branch
            git = get_git()
            git.create_branch(branch_name)

            logger.log_structured("INFO", "time",
                                 f"Branch {branch_name} from {snapshot_id}")
            return True

        except Exception as e:
            logger.log_error("time", "Branch from snapshot failed", exc=e)
            return False

    def delete_snapshot(self, snapshot_id: str) -> bool:
        """Delete snapshot"""
        if snapshot_id not in self.snapshots:
            return False

        try:
            snapshot_dir = self._dir / snapshot_id
            if snapshot_dir.exists():
                shutil.rmtree(snapshot_dir)

            del self.snapshots[snapshot_id]
            self._save_index()

            logger.log_structured("INFO", "time",
                                 f"Deleted snapshot: {snapshot_id}")
            return True

        except Exception as e:
            logger.log_error("time", "Delete failed", exc=e)
            return False


def get_time_machine() -> TimeMachine:
    """Get singleton TimeMachine"""
    return TimeMachine()

