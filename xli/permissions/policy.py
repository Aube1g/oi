#!/usr/bin/env python3
"""
XLI Permissions — the gate every tool call passes through.

Three operating modes, matching how much rope the user wants to give the agent:

  auto      run everything except what an explicit deny rule blocks
  confirm   ask before anything that mutates the machine (writes, shells)
  readonly  refuse mutations outright; reads are always allowed

The policy is a *pure decision function*. It never prints, never prompts, never
touches the network — it returns a Decision, and the frontend (CLI, TUI, nvim)
decides how to ask the human. That separation is what lets the same policy run
headless in CI and interactively in a terminal.

Deny patterns are matched with fnmatch against the concrete resource a tool
names (a path, a command string), so `--deny '/etc/*'` or
`--deny 'rm -rf *'` does what it looks like.
"""

from __future__ import annotations

import fnmatch
import os
import shlex
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Mode(str, Enum):
    AUTO = "auto"
    CONFIRM = "confirm"
    READONLY = "readonly"

    @classmethod
    def parse(cls, value: Any) -> Mode:
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        aliases = {
            "auto": cls.AUTO,
            "yolo": cls.AUTO,
            "yes": cls.AUTO,
            "confirm": cls.CONFIRM,
            "ask": cls.CONFIRM,
            "readonly": cls.READONLY,
            "read-only": cls.READONLY,
            "ro": cls.READONLY,
        }
        if text not in aliases:
            raise ValueError(
                f"unknown permission mode {value!r}; expected one of auto|confirm|readonly"
            )
        return aliases[text]


@dataclass(slots=True)
class Decision:
    allowed: bool
    reason: str
    needs_confirmation: bool = False
    rule: str | None = None

    def __bool__(self) -> bool:
        return self.allowed

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "needs_confirmation": self.needs_confirmation,
            "rule": self.rule,
        }


ALLOW = Decision(True, "allowed")


#: Commands that are never safe to run unattended, whatever the mode says.
#: The authoritative list — and the matcher, which understands wrappers and
#: `;`-separated segments — lives in :mod:`xli.core.shell_safety`. This alias
#: exists so older imports keep working.
from xli.core.shell_safety import DANGEROUS_COMMANDS as HARD_DENY_COMMANDS  # noqa: E402


#: Tools whose arguments contain a shell command. Only these get the command
#: blocklist applied: a `grep` pattern that happens to read like `rm -rf /`
#: is data, not an instruction, and refusing it would teach the user to
#: distrust the policy.
COMMAND_TOOLS: frozenset = frozenset({"bash", "shell"})

#: Tools whose arguments are file paths they intend to change. These get the
#: project-root check; a read outside the root is normal (reading /etc/hosts
#: to debug DNS is fine), a write outside it is an escalation.
MUTATING_PATH_TOOLS: frozenset = frozenset({"write", "edit", "apply_patch", "delete", "mkdir"})

# Anything that recursively deletes or rewrites history needs a human in the loop.
CONFIRM_COMMAND_PATTERNS: tuple[str, ...] = (
    "rm -rf *",
    "rm -fr *",
    "git push --force*",
    "git push -f *",
    "git reset --hard*",
    "git clean -fd*",
    "chmod -R 777 *",
    "sudo *",
    "curl * | sh*",
    "curl * | bash*",
    "wget * | sh*",
    "wget * | bash*",
    "npm publish*",
    "pip install --index-url *",
    # Any use of sudo is an escalation: the command may be harmless
    # (`apt install`), so it is confirmed rather than refused.
    "sudo *",
    "git push*",
    "git commit*",
    "git rebase*",
    "git checkout -- *",
    "git restore *",
    "git tag -d *",
    "git branch -D *",
    "kill -9 *",
    "pkill *",
    "killall *",
    "systemctl stop *",
    "systemctl restart *",
    "docker rm *",
    "docker rmi *",
    "kubectl delete *",
    "npm install -g *",
    "pip install --user *",
    "apt-get install *",
    "apt-get remove *",
)

#: Registry tools that can change the machine. Names that no longer exist
#: ("shell", "delete", "git_commit", "mkdir") were removed: a policy list that
#: mentions tools nobody can call reads as coverage while providing none, and
#: it made every audit of this file start with "which of these are real?".
#: "git" is deliberately absent — it is read-mostly, and the mutating git
#: subcommands (commit/push/reset/clean) are caught by the command patterns
#: below, which is where a subcommand-level decision belongs.
MUTATING_TOOLS: frozenset = frozenset({
    "write", "edit", "bash", "apply_patch",
})

READONLY_TOOLS: frozenset = frozenset({
    "read", "ls", "glob", "grep", "todo", "status", "diff", "git_status", "search",
})


@dataclass
class Policy:
    """Decides whether a tool call may run.

    Attributes
    ----------
    mode:            the operating mode.
    deny:            fnmatch patterns for resources that are always refused.
    allow:           fnmatch patterns that skip the confirmation prompt in
                     `confirm` mode — an explicit user opt-in.
    root:            optional project root; paths escaping it need confirmation.
    """

    mode: Mode = Mode.CONFIRM
    deny: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)
    root: Path | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    #: When True (the default) a mutating tool pointed outside `root` asks for
    #: confirmation in every mode instead of running quietly. Set False for a
    #: workflow that legitimately edits a sibling checkout.
    strict_root: bool = True

    def __post_init__(self) -> None:
        self.mode = Mode.parse(self.mode)
        if self.root is not None:
            self.root = Path(self.root).resolve()

    # ------------------------------------------------------------- resources
    #: Argument names whose value is a resource the policy should look at.
    RESOURCE_KEYS: tuple[str, ...] = (
        "path", "file", "filename", "paths", "files",
        "command", "cmd", "args", "pattern", "url",
    )

    @classmethod
    def resources_for(cls, tool: str, args: dict[str, Any]) -> list[str]:
        """Every resource a call names, not only the first one found.

        The single-resource version checked whichever key came first in a fixed
        order. A call carrying both ``path`` and ``command`` (patch-and-run)
        was therefore judged entirely on the path, and a deny rule naming the
        command never fired.
        """
        found: list[str] = []
        for key in cls.RESOURCE_KEYS:
            value = args.get(key)
            if isinstance(value, (list, tuple)):
                found.extend(str(item) for item in value if item)
            elif value:
                found.append(str(value))
        return found or [tool]

    @staticmethod
    def resource_for(tool: str, args: dict[str, Any]) -> str:
        """The primary resource — kept for callers that display one string."""
        return Policy.resources_for(tool, args)[0]

    # -------------------------------------------------------------- checking
    def check(self, tool: str, args: dict[str, Any] | None = None) -> Decision:
        """Decide whether a call may run. Every resource is checked."""
        args = args or {}
        resources = self.resources_for(tool, args)
        decision = self._check(tool, resources)
        self.history.append(
            {
                "tool": tool,
                "resource": resources[0],
                "resources": resources,
                **decision.to_dict(),
            }
        )
        return decision

    def _check(self, tool: str, resources: list[str]) -> Decision:
        # 1. Explicit deny always wins, in every mode, on any resource.
        for pattern in self.deny:
            for resource in resources:
                if _matches(pattern, resource):
                    return Decision(False, f"blocked by deny rule {pattern!r}", rule=pattern)

        # 2. Commands that are never safe, whatever the mode says. Checked
        #    segment by segment, because `ls; dd if=... of=/dev/sda` starts
        #    with `ls` and used to sail straight through the prefix check.
        if tool in COMMAND_TOOLS:
            for resource in resources:
                hard = _dangerous_command(resource)
                if hard:
                    return Decision(
                        False, f"refusing dangerous command matching {hard!r}", rule=hard
                    )

        # 3. Tool-level hard refusals.
        if self.mode is Mode.READONLY and (tool in MUTATING_TOOLS or tool in MUTATING_PATH_TOOLS):
            return Decision(False, f"tool {tool!r} is refused in readonly mode")

        # 4. Writes that leave the project root are an escalation, in every
        #    mode. `check_path_inside_root` existed but was called by nobody,
        #    so `write /etc/...` was allowed after a plain "may I write?".
        outside = self._outside_root(tool, resources)
        if outside is not None:
            if self.mode is Mode.READONLY:
                return Decision(False, f"path {outside} is outside {self.root}")
            for pattern in self.allow:
                if _matches(pattern, str(outside)):
                    break
            else:
                return Decision(
                    True,
                    f"{outside} is outside the project root ({self.root})",
                    needs_confirmation=True,
                    rule="outside-root",
                )

        # 5. Reads are always allowed.
        if tool in READONLY_TOOLS:
            return ALLOW

        # 6. Auto mode lets everything else through.
        if self.mode is Mode.AUTO:
            return ALLOW

        # 7. Explicit allow patterns opt the call out of prompting. All
        #    resources must be pre-approved, not merely one of them.
        if self.allow and all(
            any(_matches(pattern, resource) for pattern in self.allow)
            for resource in resources
        ):
            return Decision(True, "pre-approved by allow rules")

        # 8. Confirm mode: mutations and risky commands ask first.
        if tool in MUTATING_TOOLS or tool in MUTATING_PATH_TOOLS:
            return Decision(
                True, "mutation requires confirmation", needs_confirmation=True
            )

        if tool == "git":
            joined = "git " + " ".join(resources)
            for pattern in CONFIRM_COMMAND_PATTERNS:
                if _matches(pattern, joined):
                    return Decision(
                        True,
                        f"git command matches {pattern!r}",
                        needs_confirmation=True,
                        rule=pattern,
                    )

        if tool in COMMAND_TOOLS:
            for resource in resources:
                for pattern in CONFIRM_COMMAND_PATTERNS:
                    if _matches(pattern, resource):
                        return Decision(
                            True,
                            f"command matches {pattern!r}",
                            needs_confirmation=True,
                            rule=pattern,
                        )

        return ALLOW

    def _outside_root(self, tool: str, resources: list[str]) -> Path | None:
        """The first path resource that escapes the project root, or None."""
        if not self.strict_root or self.root is None or tool not in MUTATING_PATH_TOOLS:
            return None
        for resource in resources:
            if resource == tool:
                continue
            decision = self.check_path_inside_root(resource)
            if not decision.allowed:
                return Path(resource).expanduser()
        return None

    # ------------------------------------------------------------- utilities
    def check_path_inside_root(self, path: str | os.PathLike) -> Decision:
        """Refuse writes that escape the project root by symlink or .. traversal."""
        if self.root is None:
            return ALLOW
        target = Path(path)
        resolved = (target if target.is_absolute() else self.root / target).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError:
            return Decision(
                False, f"path {resolved} is outside project root {self.root}"
            )
        return ALLOW

    def confirm(self, tool: str, args: dict[str, Any] | None = None) -> Decision:
        """Mark a previously-needs_confirmation call as approved by the user."""
        decision = Decision(True, "approved by user")
        self.history.append(
            {
                "tool": tool,
                "resource": self.resource_for(tool, args or {}),
                **decision.to_dict(),
            }
        )
        return decision

    def summary(self) -> dict[str, Any]:
        total = len(self.history)
        return {
            "mode": self.mode.value,
            "decisions": total,
            "denied": sum(1 for h in self.history if not h["allowed"]),
            "confirmed": sum(1 for h in self.history if h.get("needs_confirmation")),
            "deny_rules": list(self.deny),
            "allow_rules": list(self.allow),
        }

    def reset_history(self) -> None:
        self.history.clear()


def _matches(pattern: str, resource: str) -> bool:
    """fnmatch on the resource, and on its basename for path-like patterns."""
    if fnmatch.fnmatch(resource, pattern):
        return True
    return bool(os.path.basename(resource)) and fnmatch.fnmatch(
        os.path.basename(resource), pattern
    )


def _dangerous_command(command: str) -> str | None:
    """The hard-deny gate, delegated to the shell guard's own list.

    Keeping a second copy of the rules here is how `HARD_DENY_COMMANDS` and
    `shell_safety._BLOCKED_PATTERNS` drifted: one knew about `dd of=/dev/…`,
    the other did not, and which one you hit depended on which layer ran
    first. There is one list now (`xli.core.shell_safety`), reached through a
    function-local import so the permissions package does not depend on the
    core at import time.
    """
    from xli.core.shell_safety import first_dangerous_segment

    return first_dangerous_segment(command)


def policy_from_env(default: Mode = Mode.CONFIRM) -> Policy:
    """Build a Policy from XLI_* environment variables, for headless runs."""
    mode = Mode.parse(os.environ.get("XLI_PERMISSION_MODE", default.value))
    deny = _split(os.environ.get("XLI_DENY", ""))
    allow = _split(os.environ.get("XLI_ALLOW", ""))
    root = os.environ.get("XLI_ROOT")
    return Policy(mode=mode, deny=deny, allow=allow, root=Path(root) if root else None)


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(os.pathsep) if part.strip()]


DEFAULT_POLICY = Policy(mode=Mode.CONFIRM)
