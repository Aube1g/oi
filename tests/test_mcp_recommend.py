#!/usr/bin/env python3
"""Tests for MCP recommendation and registry persistence.

Covers four fixes:

  * xli/core/mcp_recommender.py was a byte-identical copy of
    xli/mcp/recommender.py. One copy remains.
  * get_recommender() returned a new instance every call despite being named
    like an accessor, rebuilding the registry and reopening the skills index
    each time.
  * MCPRegistry.enable()/disable() mutated an in-memory dict only, so
    mcp.set_enabled over RPC reported success and the change evaporated on the
    next start.
  * Config.save() preferred the project file and ignored XLI_CONFIG_DIR, so
    the config-dir override was honoured when reading and dropped when writing.
"""

import json

import pytest

from xli.manager.config import CONFIG_FILENAME, get_config, reset_config
from xli.mcp.recommender import MCPRecommender, get_recommender, reset_recommender
from xli.mcp.registry import SERVERS, MCPRegistry


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """A throwaway config dir, with every singleton reset before and after."""
    monkeypatch.setenv("XLI_CONFIG_DIR", str(tmp_path))
    reset_config()
    reset_recommender()
    MCPRegistry._instance = None
    yield tmp_path
    reset_config()
    reset_recommender()
    MCPRegistry._instance = None


def fresh_registry():
    """Simulate a process restart: new config, new registry."""
    reset_config()
    reset_recommender()
    MCPRegistry._instance = None
    return MCPRegistry()


# ------------------------------------------------------------------ duplicate
class TestNoDuplicateRecommender:
    def test_the_core_copy_is_gone(self):
        from pathlib import Path

        assert not Path("xli/core/mcp_recommender.py").exists()

    def test_importing_the_old_path_fails(self):
        with pytest.raises(ModuleNotFoundError):
            __import__("xli.core.mcp_recommender")

    def test_the_surviving_module_works(self):
        assert MCPRecommender is not None


# ------------------------------------------------------------------ singleton
class TestRecommenderSingleton:
    def test_get_recommender_is_stable(self, isolated):
        assert get_recommender() is get_recommender()

    def test_reset_produces_a_new_one(self, isolated):
        first = get_recommender()
        reset_recommender()
        assert get_recommender() is not first


# ----------------------------------------------------------------- relevance
class TestRecommender:
    def test_security_task_recommends_the_scanner(self, isolated):
        rec = get_recommender().recommend_for_task("scan the code for security vulnerabilities")
        assert "security_scanner" in rec["mcp_servers"]

    def test_result_shape(self, isolated):
        rec = get_recommender().recommend_for_task("fix a bug")
        assert set(rec) == {"mcp_servers", "skills", "mcp_scores"}
        assert isinstance(rec["mcp_servers"], list)
        assert isinstance(rec["skills"], list)

    def test_disabled_servers_are_never_recommended(self, isolated):
        """Correct behaviour, pinned so a later change does not break it."""
        registry = MCPRegistry()
        disabled = [n for n, i in registry.servers.items() if not i.get("enabled")]
        assert disabled, "the test assumes some servers ship disabled"

        rec = get_recommender().recommend_for_task(
            "database sql query schema http api documentation"
        )
        assert not set(rec["mcp_servers"]) & set(disabled)

    def test_enabling_makes_a_server_recommendable(self, isolated):
        get_recommender().recommend_for_task("fix the slow sql query")
        MCPRegistry().enable("db_client")
        reset_recommender()
        rec = get_recommender().recommend_for_task("fix the slow sql query")
        assert "db_client" in rec["mcp_servers"]

    def test_scores_are_bounded(self, isolated):
        registry = MCPRegistry()
        task = "security test debug refactor file git database http doc format env"
        for name in registry.list_enabled():
            score = get_recommender()._score_mcp_relevance(task, name)
            assert 0.0 <= score <= 1.0

    def test_unknown_server_scores_zero(self, isolated):
        assert get_recommender()._score_mcp_relevance("anything", "no_such_server") == 0.0

    def test_context_block_is_nonempty(self, isolated):
        block = get_recommender().build_full_context("fix a bug", "coder")
        assert isinstance(block, str) and block


# ------------------------------------------------------------------ registry
class TestRegistry:
    def test_does_not_mutate_the_module_level_table(self, isolated):
        """A disabled server must not stay disabled for later registries."""
        before = SERVERS["knowledge"]["enabled"]
        MCPRegistry().disable("knowledge")
        assert SERVERS["knowledge"]["enabled"] == before

    def test_enable_and_disable_return_bools(self, isolated):
        registry = MCPRegistry()
        assert registry.enable("knowledge") is True
        assert registry.disable("knowledge") is True
        assert registry.enable("no_such_server") is False
        assert registry.disable("no_such_server") is False

    def test_enable_persists_across_a_restart(self, isolated):
        """The bug: this used to evaporate on the next start."""
        assert MCPRegistry().get_server("db_client")["enabled"] is False
        MCPRegistry().enable("db_client")

        assert fresh_registry().get_server("db_client")["enabled"] is True

    def test_disable_persists_across_a_restart(self, isolated):
        assert MCPRegistry().get_server("knowledge")["enabled"] is True
        MCPRegistry().disable("knowledge")

        assert fresh_registry().get_server("knowledge")["enabled"] is False

    def test_config_records_both_lists(self, isolated):
        MCPRegistry().disable("knowledge")
        MCPRegistry().enable("db_client")

        raw = json.loads((isolated / CONFIG_FILENAME).read_text(encoding="utf-8"))
        assert "knowledge" in raw["mcp"]["disabled"]
        assert "db_client" in raw["mcp"]["enabled_servers"]

    def _mcp_section(self, isolated):
        """Read back the mcp section, tolerating an all-defaults config.

        save() writes only what differs from the defaults, so once the state is
        back to defaults the file legitimately contains no "mcp" key at all.
        """
        raw = json.loads((isolated / CONFIG_FILENAME).read_text(encoding="utf-8"))
        return raw.get("mcp", {})

    def test_re_enable_removes_from_disabled(self, isolated):
        registry = MCPRegistry()
        registry.disable("knowledge")
        registry.enable("knowledge")

        assert "knowledge" not in self._mcp_section(isolated).get("disabled", [])

    def test_a_ship_enabled_server_is_not_added_to_enabled_servers(self, isolated):
        """Only servers that ship disabled need the explicit override."""
        MCPRegistry().disable("knowledge")
        MCPRegistry().enable("knowledge")

        assert "knowledge" not in self._mcp_section(isolated).get("enabled_servers", [])

    def test_back_to_defaults_writes_an_empty_config(self, isolated):
        """Pins the diff-only save behaviour the two tests above rely on."""
        registry = MCPRegistry()
        registry.disable("knowledge")
        registry.enable("knowledge")
        assert json.loads((isolated / CONFIG_FILENAME).read_text(encoding="utf-8")) == {}

    def test_list_enabled_reflects_changes(self, isolated):
        registry = MCPRegistry()
        registry.disable("knowledge")
        assert "knowledge" not in registry.list_enabled()

    def test_unreadable_config_does_not_break_the_registry(self, isolated):
        (isolated / CONFIG_FILENAME).write_text("{not json", encoding="utf-8")
        reset_config()
        MCPRegistry._instance = None
        assert len(MCPRegistry().servers) == len(SERVERS)


# ---------------------------------------------------------------- config save
class TestConfigSaveHonoursOverride:
    def test_save_writes_into_xli_config_dir(self, isolated, monkeypatch):
        """It used to write the project config and ignore the override."""
        config = get_config()
        config.set("mcp.disabled", ["knowledge"])
        written = config.save()

        assert written == isolated / CONFIG_FILENAME
        assert written.exists()

    def test_save_does_not_touch_the_repository(self, isolated, monkeypatch):
        monkeypatch.chdir(isolated)
        config = get_config()
        config.set("mcp.disabled", ["knowledge"])
        config.save()
        # the override dir holds it; nothing is written relative to cwd
        assert (isolated / CONFIG_FILENAME).exists()

    def test_explicit_path_still_works(self, isolated, tmp_path):
        target = tmp_path / "elsewhere" / "config.json"
        config = get_config()
        config.set("mcp.disabled", ["knowledge"])
        assert config.save(target) == target
        assert target.exists()

    def test_only_non_defaults_are_written(self, isolated):
        config = get_config()
        config.set("mcp.disabled", ["knowledge"])
        written = config.save()
        raw = json.loads(written.read_text(encoding="utf-8"))
        assert raw == {"mcp": {"disabled": ["knowledge"]}}

    def test_round_trip(self, isolated):
        config = get_config()
        config.set("mcp.disabled", ["knowledge", "lsp"])
        config.save()

        reset_config()
        assert get_config().get("mcp.disabled") == ["knowledge", "lsp"]


# ----------------------------------------------------------------------- rpc
class TestRpc:
    def _call(self, kernel, method, params=None):
        import asyncio

        from xli.kernel.protocol import Request, encode_line

        out = asyncio.run(
            kernel.feed_line(encode_line(Request(method=method, id=1, params=params or {})))
        )
        return out[0]

    @pytest.fixture
    def kernel(self, isolated):
        from xli.kernel.methods import build_kernel

        return build_kernel(project_root=isolated)

    def test_recommend_for_task(self, kernel):
        result = self._call(kernel, "recommend.for_task", {"task": "security audit"})
        assert result.error is None
        assert "mcp_servers" in result.result

    def test_recommend_requires_a_task(self, kernel):
        assert self._call(kernel, "recommend.for_task", {}).error is not None

    def test_recommend_context(self, kernel):
        result = self._call(kernel, "recommend.context", {"task": "fix a bug"})
        assert result.result["context"]

    def test_mcp_servers_lists_all(self, kernel):
        result = self._call(kernel, "mcp.servers")
        assert len(result.result["servers"]) == len(SERVERS)

    def test_set_enabled_persists(self, kernel):
        result = self._call(
            kernel, "mcp.set_enabled", {"name": "db_client", "enabled": True}
        )
        assert result.result["enabled"] is True
        # and it survives the simulated restart
        assert fresh_registry().get_server("db_client")["enabled"] is True

    def test_set_enabled_requires_both_args(self, kernel):
        assert self._call(kernel, "mcp.set_enabled", {"name": "db_client"}).error is not None

    def test_new_methods_are_listed(self, kernel):
        names = {m["name"] for m in self._call(kernel, "rpc.methods").result["methods"]}
        assert {
            "recommend.for_task",
            "recommend.context",
            "mcp.servers",
            "mcp.set_enabled",
        } <= names
