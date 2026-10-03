#!/usr/bin/env python3
"""XLI Shell Safety — shared guard for every subprocess.run(..., shell=True) call.

Before this module existed, each caller (BashTool, chain.py, nvim.py) kept its
own copy-pasted regex blocklist. That meant:
  - three slightly different lists to keep in sync
  - trivially bypassable patterns (e.g. "curl x|bash" with no spaces skipped
    the "curl .* | bash" regex entirely)
  - no coverage for command substitution / chaining tricks

This is still a blocklist, not a real sandbox (true isolation needs containers
or seccomp, tracked as a follow-up). But it is now *segment-aware*: a command is
split on `;`, `&&`, `||`, `|`, `&` and newlines, each segment is stripped of
its wrappers (`sudo`, `env`, `nohup`, …), and every segment is judged on its
own merits. `ls; dd if=/dev/zero of=/dev/sda` is no longer a command that
starts with `ls`.

All decisions live in :func:`is_shell_command_safe`, and the permission policy
calls :func:`first_dangerous_segment` so both gates use exactly one list.
"""

from __future__ import annotations

import re

#: Commands that are never safe to run unattended, whatever the arguments.
#: Matched against the first word of every segment (after unwrapping).
DANGEROUS_COMMANDS = (
    "mkfs",
    "mkfs.ext2", "mkfs.ext3", "mkfs.ext4", "mkfs.xfs", "mkfs.btrfs", "mkfs.fat",
    "shutdown", "reboot", "halt", "poweroff",
    "init",
    "dd",
    "fdisk", "parted", "mkswap", "swapon", "swapoff",
    "wipefs", "blkdiscard",
    "chroot",
)

#: Commands that only reach the disk through an argument, so the argument is
#: checked instead of the name (`dd if=… of=/dev/sda` is caught here, and a
#: plain `dd if=file of=backup.img` inside a project is left alone by the
#: caller's own permission prompt).
_DANGEROUS_ARGS = (
    re.compile(r"\bof\s*=\s*/dev/(sd|nvme|vd|hd|mmcblk|disk)"),
    re.compile(r">\s*/dev/(sd|nvme|vd|hd|mmcblk|disk)"),
    re.compile(r"\bmv\s+/\S*\s+/dev/null"),
)

#: Destructive shapes, matched against a normalized (quotes stripped, spaces
#: collapsed, lowercased) command. Ordered roughly by how bad they are.
_BLOCKED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern))
    for label, pattern in (
        ("rm -rf /", r"\brm\s+(-[a-z]*\s+)*-?[a-z]*r[a-z]*f?\s+/(\s|$|\*)"),
        ("rm -rf ~", r"\brm\s+-[a-z]*r[a-z]*f?\s+~(\s|$|/)"),
        ("rm -rf .", r"\brm\s+-[a-z]*r[a-z]*f?\s+\.(\s|$|\*)"),
        ("rm -rf $HOME", r"\brm\s+-[a-z]*r[a-z]*f?\s+\$home(\s|$|/)"),
        ("rm -rf /*", r"\brm\s+-[a-z]*r[a-z]*f?\s+/\*"),
        ("rm -rf /{usr,etc,…}", r"\brm\s+-[a-z]*r[a-z]*f?\s+/\{(usr|etc|var|home)"),
        ("find / -delete", r"find\s+/\s+.*-delete"),
        ("find / -exec rm", r"find\s+/\s+.*-exec\s+rm"),
        ("chown -R … /", r"chown\s+-[a-z]*r[a-z]*\s+[^ ]+\s+/(\s|$)"),
        ("chmod -R … /", r"chmod\s+-[a-z]*r[a-z]*\s+[0-7]{3,4}\s+/(\s|$)"),
        ("fork bomb", r":\(\)\s*\{.*\};\s*:"),
        ("mkfs", r"\bmkfs(\.|\s|$)"),
        ("dd if=…", r"\bdd\s+if="),
        ("> /dev/disk", r">\s*/dev/(sd|nvme|vd|hd|mmcblk|disk)"),
        ("> /etc/passwd", r">\s*/etc/(passwd|shadow|sudoers|fstab)"),
        ("shred/wipefs", r"\b(shred|wipefs|blkdiscard)\b"),
        # `sudo apt install x` is a *request for escalation* — the user should
        # be asked (see the policy's confirm patterns), not refused outright.
        # `sudo rm -rf` is refused, because the verb is destructive anyway.
        ("sudo <destructive>", r"\bsudo\s+(rm|chmod|chown|dd|mkfs|fdisk|parted|shred|wipefs|blkdiscard|mount|umount|su|bash|sh)\b"),
        ("rm -rf /<system dir>", r"\brm\s+-[a-z]*r[a-z]*f?\s+/(home|usr|etc|var|boot|bin|sbin|lib|opt|srv|sys|proc)(/|\s|$)"),
        ("kill -9 -1", r"\bkill\s+-9\s+-1\b"),
        ("history -c", r"\bhistory\s+-c\b"),
        ("iptables -F", r"\biptables\s+-f\b"),
        ("crontab -r", r"\bcrontab\s+-r\b"),
        ("git push --force", r"\bgit\s+push\s+(--force|-f)\b"),
        ("git reset --hard", r"\bgit\s+reset\s+--hard\b"),
        ("git clean -fd", r"\bgit\s+clean\s+-[a-z]*f[a-z]*d"),
    )
)

#: Remote-fetch tools combined with a pipe/substitution: `curl … | sh` and
#: friends. The pipe target may be `sh`, `bash`, `sudo bash`, or nothing at all
#: (`curl x | python -`).
_FETCH = re.compile(r"\b(curl|wget|fetch|nc|ncat|http)\b")
_PIPE_TO_SHELL = re.compile(
    r"\|\s*(sudo\s+)?(ba|z|k|da)?sh\b|\|\s*(sudo\s+)?(python3?|perl|ruby|node)\b"
)
#: Base64/echo puzzle that decodes to something and pipes it into a shell.
_ENCODED_PIPE = re.compile(r"\b(base64|xxd|openssl)\b.*\|\s*(sudo\s+)?(ba|z|k)?sh\b")
_SUBSTITUTION = re.compile(r"\$\(|`")

#: Commands whose arguments are *data*, not further commands. `echo 'rm -rf /'`
#: writes a note; `grep -rn 'dd if=' .` searches for a pattern. Refusing those
#: would make the guard noisy enough that a user turns it off.
_DATA_COMMANDS = frozenset({
    "echo", "printf", "tee", "logger", "cat", "grep", "egrep", "fgrep", "rg",
    "ag", "ack", "diff", "sed", "awk", "sort", "uniq", "wc", "head", "tail",
    "git",  # handled per-flag below: a commit message is data, a push is not
})
#: Flags after which the next quoted word is a message, not a command.
_MESSAGE_FLAGS = ("-m", "--message", "-e", "--expression", "-q", "--query", "-n")

#: Wrappers that hide the real command: `sudo rm …` must be judged as `rm …`.
_WRAPPERS = frozenset({"sudo", "doas", "env", "nohup", "time", "nice", "ionice", "command", "exec", "stdbuf", "setsid"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _normalize(command: str) -> str:
    """Lowercase, strip quoting and backslashes, collapse whitespace.

    Quoting is what made the first version of this module bypassable:
    ``rm -r"f" /`` did not match a pattern looking for ``rm -rf /``. The
    normalized form is used for matching only — never for execution.
    """
    text = command.strip().lower()
    text = re.sub(r"\\\s*\n", " ", text)      # line continuations
    text = text.replace('"', "").replace("'", "").replace("\\", "")
    return re.sub(r"\s+", " ", text).strip()


def segments(command: str) -> list[str]:
    """Split a shell line into the commands it actually runs, in order."""
    pieces = re.split(r"(?:\|\||&&|;|\||&|\n|\r)", command)
    return [piece.strip() for piece in pieces if piece and piece.strip()]


def _strip_wrappers(segment: str) -> str:
    """Drop `sudo`, `env VAR=x`, `nohup` … so the real verb is first."""
    text = segment.strip()
    for _ in range(6):
        match = re.match(r"^([A-Za-z_./-]+)\s*(.*)$", text)
        if not match:
            break
        head, rest = match.group(1), match.group(2)
        if head in _WRAPPERS:
            text = rest.strip()
            continue
        if _ASSIGNMENT.match(text):
            text = text.split(" ", 1)[1].strip() if " " in text else ""
            continue
        break
    return text


def _mask_literals(command: str) -> str:
    """Blank out quoted text that is data, keeping the original separators.

    Two views of a command are needed and they answer different questions:
    the dequoted view catches `rm -r"f" /`, the masked view stops a commit
    message that merely *mentions* `rm -rf /` from being refused. The
    separators are preserved because some patterns (`:(){ :|:& };:` the fork
    bomb) are only recognisable as a whole line.
    """
    pieces = re.split(r"(\|\||&&|;|\||&|\n|\r)", command)
    out: list[str] = []
    for index, piece in enumerate(pieces):
        if index % 2:  # a separator
            out.append(piece)
            continue
        out.append(_mask_segment(piece))
    return "".join(out)


def _mask_segment(segment: str) -> str:
    unwrapped = _strip_wrappers(segment)
    verb = unwrapped.split(" ", 1)[0] if unwrapped else ""
    if verb not in _DATA_COMMANDS:
        return segment
    masked = segment
    for flag in _MESSAGE_FLAGS:
        if verb == "git" and flag in ("-n", "-q"):
            continue
        pattern = re.compile(re.escape(flag) + r"""\s+("[^"]*"|'[^']*')""")
        masked = pattern.sub(flag + " MESSAGE", masked)
    if verb != "git":
        # For a data command every quoted argument is text, never a command.
        masked = re.sub(r"""("[^"]*"|'[^']*')""", "LITERAL", masked)
    return masked


def first_dangerous_segment(command: str) -> str | None:
    """Return the offending token/pattern if this command is never safe.

    Used by the permission policy as its hard-deny gate and by
    :func:`is_shell_command_safe` as the first check — one list, two entry
    points, so the two can never drift apart again.
    """
    if not command or not command.strip():
        return None
    normalized = _normalize(command)
    # Data first (quoted prose is not a command), then dequoting: `rm -r"f" /`
    # still has to read as `rm -rf /` once its quotes are gone.
    masked = _normalize(_mask_literals(command))

    for segment in segments(masked):
        unwrapped = _strip_wrappers(segment)
        first = unwrapped.split(" ", 1)[0] if unwrapped else ""
        if first in DANGEROUS_COMMANDS:
            return first
        for pattern in _DANGEROUS_ARGS:
            if pattern.search(unwrapped):
                return "write to a block device"

    # `cd / && rm -rf *` is the classic way to delete a machine: each half is
    # ordinary on its own, and together they wipe the root. Tracked across the
    # segment list rather than by a regex over the whole line, because the two
    # halves may be separated by anything.
    moved_to_root = False
    for segment in segments(masked):
        stripped = _strip_wrappers(segment)
        if stripped.startswith("cd ") and stripped[3:].strip() in ("/", "~", "$home", "${home}"):
            moved_to_root = True
            continue
        if moved_to_root and re.match(r"rm\s+(-[a-z]+\s+)*-?[a-z]*r[a-z]*f?\s+(\.?/)?\*(\s|$)", stripped):
            return "rm -rf * (after cd /)"

    for label, pattern in _BLOCKED_PATTERNS:
        if pattern.search(masked):
            return label
    return None


def is_shell_command_safe(command: str) -> tuple[bool, str | None]:
    """Check a raw shell command before it is passed to subprocess.run(shell=True).

    Returns (True, None) if the command looks acceptable, else (False, reason).
    This is a defense-in-depth heuristic, not a guarantee — treat any command
    from an LLM or untrusted source as something that could still do damage
    within the permissions of the running process.
    """
    if not command or not command.strip():
        return False, "empty command"

    danger = first_dangerous_segment(command)
    if danger:
        return False, f"blocked: {danger}"

    normalized = _normalize(command)
    # Data first (quoted prose is not a command), then dequoting: `rm -r"f" /`
    # still has to read as `rm -rf /` once its quotes are gone.
    masked = _normalize(_mask_literals(command))
    if _ENCODED_PIPE.search(masked):
        return False, "base64/openssl output piped into a shell"
    if _FETCH.search(masked) and _PIPE_TO_SHELL.search(masked):
        return False, "remote fetch piped into a shell"
    if _FETCH.search(normalized) and _SUBSTITUTION.search(command):
        return False, "remote fetch combined with command substitution"
    return True, None


__all__ = [
    "DANGEROUS_COMMANDS",
    "first_dangerous_segment",
    "is_shell_command_safe",
    "segments",
]
