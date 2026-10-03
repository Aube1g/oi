#!/usr/bin/env python3
"""Built-in sub-agent specs.

These ship with xli so a fresh install has something to delegate to, and so
`xli agents create` has a shape to copy. They are deliberately narrow: each one
names the smallest tool set that can do its job, because the point of a
sub-agent is that it cannot wander.

A user or project file with the same name overrides the entry here without
deleting it, so the built-ins are a floor rather than a ceiling.
"""

from __future__ import annotations

from xli.agents.spec import AgentSpec

BUILTIN_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec(
        name="reviewer",
        description="Reads a diff or a file and reports defects without changing anything.",
        role=(
            "You review code. Read the files you are pointed at, then report concrete "
            "defects: incorrect logic, unhandled errors, mismatched types, tests that "
            "assert nothing. Cite file and line. Do not propose a rewrite; say what is "
            "wrong and why it matters."
        ),
        tools=["read", "ls", "glob", "grep"],
        mode="readonly",
        max_steps=10,
        temperature=0.2,
        colour="accent",
        tags=["review", "readonly"],
    ),
    AgentSpec(
        name="test-writer",
        description="Writes failing tests that pin down a reported bug before it is fixed.",
        role=(
            "You write tests. Given a bug report or a function, produce a test that fails "
            "for the reported reason and passes once it is fixed. Use the project's own "
            "test runner and layout. Never weaken an existing assertion to make a test "
            "pass, and never mark a test skipped to get a green run."
        ),
        tools=["read", "write", "ls", "glob", "grep", "bash"],
        mode="confirm",
        max_steps=14,
        temperature=0.3,
        colour="good",
        tags=["tests"],
    ),
    AgentSpec(
        name="debugger",
        description="Reproduces a failure and narrows it to a specific line or call.",
        role=(
            "You debug. Reproduce the failure first; a bug you cannot trigger is a bug you "
            "cannot claim to have found. Then narrow it: read the code path, add the "
            "minimum instrumentation, and name the exact line and the exact value that is "
            "wrong. Report the cause, not a guess."
        ),
        tools=["read", "ls", "glob", "grep", "bash"],
        mode="confirm",
        max_steps=16,
        temperature=0.3,
        colour="warn",
        tags=["debug"],
    ),
    AgentSpec(
        name="explorer",
        description="Maps an unfamiliar codebase and reports how the parts fit together.",
        role=(
            "You explore. Build an accurate map of the code you are pointed at: entry "
            "points, the modules they reach, where configuration is read, where state is "
            "written. Say what you verified and what you inferred. Do not describe a file "
            "you did not open."
        ),
        tools=["read", "ls", "glob", "grep"],
        mode="readonly",
        max_steps=12,
        temperature=0.2,
        colour="accent",
        tags=["explore", "readonly"],
    ),
    AgentSpec(
        name="documenter",
        description="Writes or corrects documentation from what the code actually does.",
        role=(
            "You document. Read the implementation before writing a word about it. "
            "Document behaviour you verified, including the failure modes, and leave out "
            "anything you could not confirm. Prefer a short accurate sentence to a long "
            "plausible one."
        ),
        tools=["read", "write", "edit", "ls", "glob", "grep"],
        mode="confirm",
        max_steps=12,
        temperature=0.4,
        colour="accent",
        tags=["docs"],
    ),
)

#: Names, for callers that only need to know what ships.
BUILTIN_NAMES: tuple[str, ...] = tuple(spec.name for spec in BUILTIN_SPECS)
