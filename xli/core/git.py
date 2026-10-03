#!/usr/bin/env python3
"""
XLI Git Integration v4 — Auto-commit, branch, stash, diff
"""

import subprocess
from pathlib import Path
from datetime import datetime

from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.git")


class GitIntegration:
    """Git operations for XLI"""

    def __init__(self, repo_path: str | None = None):
        self.repo_path = Path(repo_path) if repo_path else Path.cwd()
        self._check_git()

    def _check_git(self):
        """Check if directory is git repo"""
        git_dir = self.repo_path / ".git"
        if not git_dir.exists():
            logger.log_structured("WARN", "git", "Not a git repository")

    def _run(self, cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
        """Run git command"""
        full_cmd = ["git", "-C", str(self.repo_path)] + cmd
        try:
            result = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                check=check,
                timeout=30
            )
            return result
        except subprocess.CalledProcessError as e:
            logger.log_error("git", f"Git command failed: {' '.join(cmd)}",
                            details={"stderr": e.stderr})
            raise
        except Exception as e:
            logger.log_error("git", f"Git error: {e}")
            raise

    def get_status(self) -> dict[str, list[str]]:
        """Get git status"""
        try:
            result = self._run(["status", "--porcelain"], check=False)

            modified = []
            untracked = []
            staged = []

            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                status = line[:2]
                file = line[3:].strip()

                if status.startswith("M") or status.startswith(" A"):
                    staged.append(file)
                elif status.startswith(" M") or status.startswith(" D"):
                    modified.append(file)
                elif status.startswith("??"):
                    untracked.append(file)

            return {
                "modified": modified,
                "untracked": untracked,
                "staged": staged
            }

        except Exception as e:
            logger.log_error("git", "Status failed", exc=e)
            return {"modified": [], "untracked": [], "staged": []}

    def auto_commit(self, message: str | None = None) -> str:
        """Auto-commit all changes"""
        if not message:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            message = f"XLI auto-commit {timestamp}"

        try:
            # Stage all
            self._run(["add", "-A"])

            # Commit
            result = self._run(["commit", "-m", message])

            logger.log_structured("INFO", "git", f"Auto-committed: {message}")
            return f"Committed: {message}"

        except Exception as e:
            logger.log_error("git", "Auto-commit failed", exc=e)
            return f"Failed: {e}"

    def create_branch(self, name: str, checkout: bool = True) -> str:
        """Create and optionally checkout branch"""
        try:
            self._run(["branch", name])
            if checkout:
                self._run(["checkout", name])
            logger.log_structured("INFO", "git", f"Branch created: {name}")
            return f"Branch: {name}"
        except Exception as e:
            logger.log_error("git", "Branch creation failed", exc=e)
            return f"Failed: {e}"

    def stash(self, message: str | None = None) -> str:
        """Stash changes"""
        try:
            cmd = ["stash", "push"]
            if message:
                cmd.extend(["-m", message])
            self._run(cmd)
            logger.log_structured("INFO", "git", "Stashed changes")
            return "Stashed"
        except Exception as e:
            logger.log_error("git", "Stash failed", exc=e)
            return f"Failed: {e}"

    def unstash(self) -> str:
        """Pop stash"""
        try:
            self._run(["stash", "pop"])
            logger.log_structured("INFO", "git", "Unstashed")
            return "Unstashed"
        except Exception as e:
            logger.log_error("git", "Unstash failed", exc=e)
            return f"Failed: {e}"

    def get_diff_since_last(self) -> str:
        """Get diff since last commit"""
        try:
            result = self._run(["diff", "HEAD~1", "HEAD"], check=False)
            return result.stdout
        except Exception as e:
            logger.log_error("git", "Diff failed", exc=e)
            return ""

    def get_log(self, limit: int = 10) -> list[dict]:
        """Get commit log"""
        try:
            result = self._run([
                "log", f"-{limit}",
                "--pretty=format:%H|%s|%an|%ad",
                "--date=short"
            ], check=False)

            commits = []
            for line in result.stdout.strip().split("\n"):
                if "|" in line:
                    parts = line.split("|")
                    commits.append({
                        "hash": parts[0][:8],
                        "message": parts[1],
                        "author": parts[2],
                        "date": parts[3]
                    })

            return commits

        except Exception as e:
            logger.log_error("git", "Log failed", exc=e)
            return []


def get_git(repo_path: str | None = None) -> GitIntegration:
    """Get GitIntegration instance"""
    return GitIntegration(repo_path)

