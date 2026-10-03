#!/usr/bin/env python3
"""Regression tests for the skills index.

These cover the three bugs found in the scanner: it indexed an empty user
directory instead of the bundled corpus, it compared a datetime against an
isoformat string so every run re-read every file, and FTS4 inserts on an
existing name duplicated rows instead of replacing them.
"""

from datetime import datetime
from pathlib import Path

import pytest

from xli.core import skills as skills_module
from xli.core.skills import SkillsManager, get_skills_manager


@pytest.fixture
def isolated_index(tmp_path, monkeypatch):
    """A SkillsManager pointed at throwaway directories with known contents."""
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    db = tmp_path / "skills.db"
    for directory in (bundled, user):
        directory.mkdir()

    monkeypatch.setattr(skills_module, "BUNDLED_SKILLS_DIR", bundled)
    monkeypatch.setattr(skills_module, "SKILLS_DIR", user)
    monkeypatch.setattr(skills_module, "SKILLS_DB", db)

    # The manager is a singleton with an _initialized guard; a fresh instance
    # is the only way to get a scan against the patched paths.
    manager = SkillsManager.__new__(SkillsManager)
    manager._initialized = False
    return manager, bundled, user


def write_skill(directory: Path, folder: str, name: str, body: str) -> Path:
    target = directory / folder
    target.mkdir(parents=True, exist_ok=True)
    path = target / "SKILL.md"
    path.write_text(body, encoding="utf-8")
    return path


# ------------------------------------------------------------------- scanning
class TestScanning:
    def test_bundled_skills_are_indexed(self, isolated_index):
        manager, bundled, _ = isolated_index
        write_skill(bundled, "pytest-skill", "SKILL", "how to run pytest and read failures")

        manager._init_db()
        manager._scan_skills()

        names = [row[0] for row in manager.list_skills()]
        assert "pytest-skill_SKILL" in names

    def test_user_skills_are_indexed_too(self, isolated_index):
        manager, bundled, user = isolated_index
        write_skill(bundled, "a-skill", "SKILL", "bundled one")
        write_skill(user, "mine", "SKILL", "user one")

        manager._init_db()
        manager._scan_skills()

        names = [row[0] for row in manager.list_skills()]
        assert {"a-skill_SKILL", "mine_SKILL"} <= set(names)

    def test_user_skill_overrides_bundled_of_same_name(self, isolated_index):
        manager, bundled, user = isolated_index
        write_skill(bundled, "shared", "SKILL", "bundled version")
        write_skill(user, "shared", "SKILL", "user version wins")

        manager._init_db()
        manager._scan_skills()

        content = manager.load_skill("shared_SKILL")
        assert content == "user version wins"

    def test_empty_index_when_no_skills_anywhere(self, isolated_index):
        manager, _, _ = isolated_index
        manager._init_db()
        manager._scan_skills()
        assert manager.list_skills() == []

    def test_deleted_skill_is_dropped_on_rescan(self, isolated_index):
        manager, bundled, _ = isolated_index
        path = write_skill(bundled, "temp-skill", "SKILL", "here today")

        manager._init_db()
        manager._scan_skills()
        assert [r[0] for r in manager.list_skills()] == ["temp-skill_SKILL"]

        path.unlink()
        manager._scan_skills()
        assert manager.list_skills() == []

    def test_hidden_and_underscore_files_skipped(self, isolated_index):
        manager, bundled, _ = isolated_index
        (bundled / ".hidden.md").write_text("secret")
        (bundled / "_draft.md").write_text("draft")
        write_skill(bundled, "real", "SKILL", "indexed")

        manager._init_db()
        manager._scan_skills()

        assert [r[0] for r in manager.list_skills()] == ["real_SKILL"]

    def test_real_repository_corpus_is_not_empty(self):
        """Guards the original bug: the shipped skills must actually be found."""
        manager = get_skills_manager()
        assert len(manager.list_skills()) > 100


class TestRescanIsCheap:
    def test_unchanged_files_are_not_reread(self, isolated_index):
        """The mtime check must actually match, or every run re-reads everything."""
        manager, bundled, _ = isolated_index
        write_skill(bundled, "stable", "SKILL", "never changes")

        manager._init_db()
        manager._scan_skills()

        # A second scan with nothing changed must not rewrite the row; if the
        # mtime comparison were broken the modified column would move.
        import sqlite3

        before = sqlite3.connect(str(skills_module.SKILLS_DB)).execute(
            "SELECT modified FROM skills WHERE name = 'stable_SKILL'"
        ).fetchone()[0]

        manager._scan_skills()

        after = sqlite3.connect(str(skills_module.SKILLS_DB)).execute(
            "SELECT modified FROM skills WHERE name = 'stable_SKILL'"
        ).fetchone()[0]

        assert before == after
        # ...and it is stored as comparable text, not a datetime repr.
        datetime.fromisoformat(before)


# --------------------------------------------------------------------- search
class TestSearch:
    @pytest.fixture
    def indexed(self, isolated_index):
        manager, bundled, _ = isolated_index
        write_skill(bundled, "pytest-guide", "SKILL", "pytest fixtures, markers and assertions " * 3)
        write_skill(bundled, "kubernetes", "SKILL", "kubectl, pods and deployments")
        write_skill(bundled, "unrelated", "SKILL", "something about cooking pasta")

        manager._init_db()
        manager._scan_skills()
        return manager

    def test_relevant_hit_outranks_irrelevant(self, indexed):
        results = indexed.search_skills("pytest", limit=3)
        names = [name for name, _, _ in results]
        assert names[0] == "pytest-guide_SKILL"
        assert "unrelated_SKILL" not in names

    def test_scores_are_nonzero(self, indexed):
        """The original query hardcoded 0.0, so callers could not rank anything."""
        results = indexed.search_skills("pytest", limit=3)
        assert results
        assert all(score > 0 for _, _, score in results)

    def test_results_sorted_by_score_descending(self, indexed):
        results = indexed.search_skills("kubectl pods", limit=5)
        scores = [score for _, _, score in results]
        assert scores == sorted(scores, reverse=True)

    def test_empty_query_returns_nothing(self, indexed):
        assert indexed.search_skills("") == []
        assert indexed.search_skills("   ") == []

    def test_multi_term_query(self, indexed):
        results = indexed.search_skills("kubectl deployment", limit=3)
        assert results[0][0] == "kubernetes_SKILL"

    def test_no_match_returns_empty(self, indexed):
        assert indexed.search_skills("zzzznotaword") == []

    def test_fts_syntax_in_input_does_not_crash(self, indexed):
        # Quotes, stars and NEAR are FTS operators; they must be treated as text.
        for query in ['"unclosed', "NEAR(a b)", "col*umn", "a OR b", "!!!"]:
            assert isinstance(indexed.search_skills(query, limit=3), list)

    def test_limit_respected(self, indexed):
        assert len(indexed.search_skills("skill", limit=1)) <= 1

    def test_no_duplicate_rows_for_one_skill(self, isolated_index):
        """FTS4 has no REPLACE; a naive insert made search return duplicates."""
        manager, bundled, _ = isolated_index
        path = write_skill(bundled, "dup-check", "SKILL", "findme findme findme")

        manager._init_db()
        manager._scan_skills()
        # Force a re-index by moving the mtime forward.
        import os

        stat = path.stat()
        os.utime(path, (stat.st_atime + 10, stat.st_mtime + 10))
        manager._scan_skills()

        results = manager.search_skills("findme", limit=20)
        names = [name for name, _, _ in results]
        assert names.count("dup-check_SKILL") == 1


# -------------------------------------------------------------------- context
class TestSkillsContext:
    @pytest.fixture
    def indexed(self, isolated_index):
        manager, bundled, _ = isolated_index
        write_skill(bundled, "python-helpers", "SKILL", "python functions, classes and imports " * 4)
        write_skill(bundled, "debugging-deep", "SKILL", "debug errors, tracebacks and exceptions " * 4)

        manager._init_db()
        manager._scan_skills()
        return manager

    def test_context_nonempty_for_known_role(self, indexed):
        context = indexed.get_skills_context("CODER", max_skills=3)
        assert context
        assert "Available Skills" in context

    def test_context_truncates_long_skills(self, indexed):
        context = indexed.get_skills_context("CODER", max_skills=3)
        # Each skill body is capped, so the block cannot grow without bound.
        assert len(context) < 4000

    def test_unknown_role_falls_back(self, indexed):
        assert isinstance(indexed.get_skills_context("NOT_A_ROLE"), str)

    def test_empty_index_gives_empty_context(self, isolated_index):
        manager, _, _ = isolated_index
        manager._init_db()
        manager._scan_skills()
        assert manager.get_skills_context("CODER") == ""


# ------------------------------------------------------------------- agent wire
class TestAgentWiring:
    def test_agent_prompt_includes_skills(self):
        from xli.agent import Agent
        from xli.permissions.policy import Mode, Policy
        from xli.providers.fake import FakeProvider
        from xli.tools.registry import default_registry

        policy = Policy(mode=Mode.AUTO)
        agent = Agent(
            FakeProvider(),
            registry=default_registry(policy=policy),
            policy=policy,
            role="DEBUGGER",
        )
        prompt = agent.system_prompt()
        assert "Available Skills" in prompt
        assert "read" in prompt  # tools still listed

    def test_agent_prompt_can_opt_out(self):
        from xli.agent import Agent
        from xli.permissions.policy import Mode, Policy
        from xli.providers.fake import FakeProvider
        from xli.tools.registry import default_registry

        policy = Policy(mode=Mode.AUTO)
        agent = Agent(
            FakeProvider(), registry=default_registry(policy=policy), policy=policy
        )
        assert "Available Skills" not in agent.system_prompt(include_skills=False)

    def test_skills_context_is_cached(self):
        from xli.agent import Agent
        from xli.permissions.policy import Mode, Policy
        from xli.providers.fake import FakeProvider
        from xli.tools.registry import default_registry

        policy = Policy(mode=Mode.AUTO)
        agent = Agent(
            FakeProvider(), registry=default_registry(policy=policy), policy=policy
        )
        first = agent.skills_context
        agent._skills = "sentinel"
        assert agent.skills_context == "sentinel", "context must not be recomputed"
        assert isinstance(first, str)

    def test_broken_skills_never_break_the_agent(self, monkeypatch):
        from xli.agent import Agent
        from xli.permissions.policy import Mode, Policy
        from xli.providers.fake import FakeProvider
        from xli.tools.registry import default_registry

        def boom(*a, **k):
            raise RuntimeError("skills exploded")

        monkeypatch.setattr(skills_module, "get_skills_manager", boom)
        policy = Policy(mode=Mode.AUTO)
        agent = Agent(
            FakeProvider(), registry=default_registry(policy=policy), policy=policy
        )
        assert agent.skills_context == ""
        assert "read" in agent.system_prompt()
